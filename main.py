import os
import re
import ast
import time
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# Configuration
# ============================================================

BASE_URL = "https://api.smilepass.com/clinics"
SMILEPASS_HOME = "https://app.smilepass.com/"
PLAN_CATEGORIES = ["preventative", "basic", "major", "orthodontics"]

CLINICS = {
    81: "Smile Centre Mapleridge",
    87: "Smile Well Dental - Langley",
    11: "Parkwoods Dental",
    88: "Clarence Street Dental",
    89: "Root Cause Dental Group",
    83: "Rethink Dentistry",
}

# Put these in Deployka Environment/Secrets. Never hard-code them.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
SMILEPASS_TOKEN = os.environ.get("SMILEPASS_TOKEN", "").strip()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("smilepass_bot")


# ============================================================
# Smilepass API helpers
# ============================================================

def request_with_retry(method, url, max_retries=3, retry_delay=1.5, **kwargs):
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.request(method, url, timeout=30, **kwargs)
            if resp.status_code >= 500 and attempt < max_retries:
                time.sleep(retry_delay * (2 ** attempt))
                continue
            return resp
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc
            if attempt < max_retries:
                time.sleep(retry_delay * (2 ** attempt))
                continue
            raise
    raise last_exc


def make_headers(token):
    return {
        "Authorization": f"Token  {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://app.smilepass.com",
        "Referer": "https://app.smilepass.com/",
    }


def check_token(token):
    resp = request_with_retry(
        "GET",
        f"{BASE_URL}/insurance-providers/",
        headers=make_headers(token),
    )
    return resp.status_code == 200


def search_patient(token, name, office_id):
    resp = request_with_retry(
        "GET",
        f"{BASE_URL}/patient-list/",
        headers=make_headers(token),
        params={"page": 1, "office_id": office_id, "name": name},
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


def get_policies(token, patient_id):
    resp = request_with_retry(
        "GET",
        f"{BASE_URL}/patients/insurance-policies/",
        headers=make_headers(token),
        params={"patient_id": patient_id},
    )
    resp.raise_for_status()
    return resp.json()


def update_policy_fields(token, policy_id, fee_guide, deductible, specialist_fee_covered):
    payload = {}
    if fee_guide is not None:
        payload["fee_guide"] = str(fee_guide)
    if deductible is not None:
        payload["deductible"] = str(deductible)
    if specialist_fee_covered is not None:
        payload["specialist_fee_covered"] = bool(specialist_fee_covered)

    if not payload:
        return

    resp = request_with_retry(
        "PATCH",
        f"{BASE_URL}/patients/update-insurance-policy/?policy_id={policy_id}",
        headers=make_headers(token),
        json=payload,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Policy fields save failed ({resp.status_code}): {resp.text[:300]}"
        )


def create_breakdown(token, patient_id, policy_id, office_id):
    payload = {
        "patient_id": patient_id,
        "policy_id": policy_id,
        "office_id": str(office_id),
    }
    resp = request_with_retry(
        "POST",
        f"{BASE_URL}/breakdown-manual-create/",
        headers=make_headers(token),
        json=payload,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def is_plan_unlimited(value):
    if value is None:
        return True
    if isinstance(value, str):
        s = value.strip().lower().replace(",", "")
        if s in {"unlimited", "none", ""}:
            return True
        try:
            return float(s) >= 9_000_000
        except ValueError:
            return False
    try:
        return float(value) >= 9_000_000
    except (TypeError, ValueError):
        return False


def update_plan_boxes(token, breakdown_id, plans, office_id):
    plans = plans or {}
    category_overrides = {}

    for category in PLAN_CATEGORIES:
        values = plans.get(category)

        if values is None:
            category_overrides[category] = {
                "overall_max": "0",
                "percentage": "0",
                "benefits_remaining": "0",
            }
            continue

        raw_max = values.get("overall_max")
        raw_pct = values.get("percentage")

        no_coverage = (
            raw_max in (None, "", "0", 0)
            and raw_pct in (None, "", "0", 0)
        )

        if no_coverage:
            max_str = "0"
        elif is_plan_unlimited(raw_max):
            max_str = "9999999.99"
        else:
            max_str = str(raw_max)

        category_overrides[category] = {
            "overall_max": max_str,
            "percentage": str(raw_pct) if raw_pct is not None else "0",
            "benefits_remaining": (
                str(values["benefits_remaining"])
                if values.get("benefits_remaining") is not None
                else None
            ),
        }

    if not category_overrides:
        return

    resp = request_with_retry(
        "PATCH",
        f"{BASE_URL}/breakdown-patient-rud/{breakdown_id}/?office_id={office_id}",
        headers=make_headers(token),
        json={"category_overrides": category_overrides},
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Plan boxes save failed ({resp.status_code}): {resp.text[:300]}"
        )


def get_procedure_mapping(token, breakdown_id):
    resp = request_with_retry(
        "GET",
        f"{BASE_URL}/breakdown-patient-details/?breakdown_id={breakdown_id}",
        headers=make_headers(token),
    )
    resp.raise_for_status()

    mapping = {}
    for entry in resp.json().get("patient_procedure_coverages", []):
        details = entry["procedure_code_details"]
        code = details["procedure_code"]
        mapping[code] = {
            "row_id": entry["id"],
            "procedure_internal_id": details["id"],
        }
    return mapping


def save_procedure(token, breakdown_id, mapping, proc, office_id):
    code = str(proc["code"])
    if code not in mapping:
        raise RuntimeError(f"Procedure {code} not found on this breakdown.")

    info = mapping[code]
    freq = proc.get("frequency")

    occurrences = int(round(freq[0])) if freq and freq[0] is not None else None
    months = int(round(freq[1])) if freq and freq[1] is not None else None

    payload = {
        "id": info["row_id"],
        "procedure_code": info["procedure_internal_id"],
        "percentage_covered": (
            str(proc["coverage"])
            if proc.get("coverage") not in (None, "")
            else None
        ),
        "overall_max": None,
        "max_occurrences": occurrences,
        "coverage_duration_months": months,
        "notes": proc.get("note") or "",
    }

    resp = request_with_retry(
        "PATCH",
        f"{BASE_URL}/patient-procedure-coverage-crud/?office_id={office_id}&breakdown_id={breakdown_id}",
        headers=make_headers(token),
        json=payload,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Procedure {code} save failed ({resp.status_code}): {resp.text[:300]}"
        )


# ============================================================
# DATA parser / validation
# ============================================================

def parse_data_text(raw_text):
    text = raw_text.strip()
    start_match = re.search(r"DATA\s*=\s*\{", text)
    if not start_match:
        raise ValueError(
            "Couldn't find a 'DATA = { ... }' block. "
            "Paste the full DATA block starting with DATA = {."
        )

    brace_start = text.index("{", start_match.start())
    depth = 0
    end_idx = None

    # The source desktop app uses brace counting. We retain that behavior.
    for i in range(brace_start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end_idx = i + 1
                break

    if end_idx is None:
        raise ValueError("The DATA block's braces do not match.")

    dict_text = text[brace_start:end_idx]

    try:
        data = ast.literal_eval(dict_text)
    except Exception as exc:
        raise ValueError(f"Could not parse DATA safely: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("DATA must be a dictionary.")

    if "procedures" not in data:
        raise ValueError("DATA is missing the required 'procedures' key.")

    procedures = data.get("procedures")
    if not isinstance(procedures, list):
        raise ValueError("'procedures' must be a list.")

    for idx, proc in enumerate(procedures, start=1):
        if not isinstance(proc, dict):
            raise ValueError(f"Procedure #{idx} must be a dictionary.")
        if not proc.get("code"):
            raise ValueError(f"Procedure #{idx} is missing 'code'.")
        if "frequency" in proc and proc["frequency"] is not None:
            freq = proc["frequency"]
            if not isinstance(freq, (list, tuple)) or len(freq) < 2:
                raise ValueError(
                    f"Procedure {proc['code']} has an invalid frequency. "
                    "Use [occurrences, months] or None."
                )

    return data


# ============================================================
# Bot state
# ============================================================

@dataclass
class Session:
    clinic_id: Optional[int] = None
    patient_matches: list = field(default_factory=list)
    patient: Optional[dict] = None
    policies: list = field(default_factory=list)
    policy: Optional[dict] = None
    waiting_for: Optional[str] = None
    progress_message_id: Optional[int] = None
    busy: bool = False


SESSIONS = {}


def session_for(user_id):
    if user_id not in SESSIONS:
        SESSIONS[user_id] = Session()
    return SESSIONS[user_id]


def reset_session(user_id):
    SESSIONS[user_id] = Session()
    return SESSIONS[user_id]


def display_patient(patient):
    first = patient.get("first_name", "")
    last = patient.get("last_name", "")
    name = f"{first} {last}".strip()
    member_id = patient.get("member_id") or patient.get("memberId") or ""
    dob = patient.get("dob") or patient.get("date_of_birth") or ""
    extra = []
    if dob:
        extra.append(f"DOB: {dob}")
    if member_id:
        extra.append(f"Member ID: {member_id}")
    return name or "Unknown patient", extra


def display_policy(policy):
    carrier = (
        policy.get("insurance_name")
        or policy.get("carrier_name")
        or policy.get("insurance_company")
        or "Unknown carrier"
    )
    typ = policy.get("insurance_type_display") or policy.get("insurance_type") or ""
    number = (
        policy.get("member_id")
        or policy.get("memberId")
        or policy.get("certificate_number")
        or policy.get("policy_number")
        or ""
    )
    parts = [carrier]
    if typ:
        parts.append(str(typ))
    if number:
        parts.append(f"ID: {number}")
    return " — ".join(parts)


def closest_patient_candidates(matches, query, limit=8):
    # Smilepass already returns the server's name search results. We use those
    # results first; this is deliberately conservative rather than inventing
    # patients that were not returned by Smilepass.
    q = re.sub(r"\s+", " ", query.strip().lower())
    tokens = set(q.split())

    def score(p):
        name, _ = display_patient(p)
        n = name.lower()
        score = 0
        if q and q in n:
            score += 100
        for token in tokens:
            if token in n:
                score += 10
        return score

    return sorted(matches, key=score, reverse=True)[:limit]


# ============================================================
# Telegram UI helpers
# ============================================================

async def safe_edit(bot, chat_id, message_id, text, reply_markup=None):
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text[:4090],
            reply_markup=reply_markup,
        )
    except Exception as exc:
        # Telegram raises an error when the text is identical. That is harmless.
        if "Message is not modified" not in str(exc):
            logger.warning("Progress edit failed: %s", exc)


async def progress(update, context, session, text):
    if session.progress_message_id:
        await safe_edit(
            context.bot,
            update.effective_chat.id,
            session.progress_message_id,
            text,
        )
    else:
        msg = await update.effective_chat.send_message(text)
        session.progress_message_id = msg.message_id


def clinic_keyboard():
    rows = []
    for clinic_id, name in CLINICS.items():
        rows.append([
            InlineKeyboardButton(name, callback_data=f"clinic:{clinic_id}")
        ])
    return InlineKeyboardMarkup(rows)


def patient_keyboard(matches):
    rows = []
    for i, patient in enumerate(matches):
        name, extra = display_patient(patient)
        label = name
        if extra:
            label += f" ({extra[0]})"
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"patient:{i}")])
    return InlineKeyboardMarkup(rows)


def policy_keyboard(policies):
    rows = []
    for i, policy in enumerate(policies):
        rows.append([
            InlineKeyboardButton(display_policy(policy)[:60], callback_data=f"policy:{i}")
        ])
    return InlineKeyboardMarkup(rows)


# ============================================================
# Commands / conversation flow
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    reset_session(user_id)

    await update.message.reply_text(
        "Smilepass Breakdown Bot\n\n"
        "Choose the clinic:",
        reply_markup=clinic_keyboard(),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_session(update.effective_user.id)
    await update.message.reply_text(
        "Cancelled.\n\nChoose a clinic to start again:",
        reply_markup=clinic_keyboard(),
    )


async def clinic_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    session = reset_session(user_id)

    clinic_id = int(query.data.split(":", 1)[1])
    session.clinic_id = clinic_id
    session.waiting_for = "patient_name"

    await query.edit_message_text(
        f"Clinic selected:\n{CLINICS[clinic_id]}\n\n"
        "Now send the patient's full name."
    )


async def patient_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    session = session_for(user_id)

    if query.data == "patient_confirm":
        patient = session.patient
        if not patient:
            await query.edit_message_text("Patient selection expired. Send /start.")
            return
        await continue_after_patient(update, context, session, patient, edit_query=True)
        return

    if query.data == "patient_cancel":
        session.patient = None
        session.patient_matches = []
        session.waiting_for = "patient_name"
        await query.edit_message_text("Send the patient's name again.")
        return

    idx = int(query.data.split(":", 1)[1])
    if idx < 0 or idx >= len(session.patient_matches):
        await query.edit_message_text("Selection expired. Send /start.")
        return

    patient = session.patient_matches[idx]
    await continue_after_patient(update, context, session, patient, edit_query=True)


async def continue_after_patient(update, context, session, patient, edit_query=False):
    session.patient = patient
    session.waiting_for = None

    name, extra = display_patient(patient)
    text = f"Patient selected:\n{name}"
    if extra:
        text += "\n" + "\n".join(extra)
    text += "\n\nFetching insurance policies..."

    if edit_query:
        await update.callback_query.edit_message_text(text)
    else:
        await update.message.reply_text(text)

    try:
        session.policies = get_policies(SMILEPASS_TOKEN, patient["id"])
    except Exception as exc:
        await update.effective_chat.send_message(f"Failed to get insurance policies:\n{exc}")
        return

    if not session.policies:
        await update.effective_chat.send_message(
            f"{name} has no insurance policies on file."
        )
        return

    if len(session.policies) == 1:
        session.policy = session.policies[0]
        await ask_for_data(update, context, session)
    else:
        session.waiting_for = "policy"
        await update.effective_chat.send_message(
            f"{name} has multiple insurance policies.\n\n"
            "Choose the carrier/policy to use:",
            reply_markup=policy_keyboard(session.policies),
        )


async def policy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    session = session_for(update.effective_user.id)
    idx = int(query.data.split(":", 1)[1])

    if idx < 0 or idx >= len(session.policies):
        await query.edit_message_text("Selection expired. Send /start.")
        return

    session.policy = session.policies[idx]
    session.waiting_for = None

    await query.edit_message_text(
        f"Policy selected:\n{display_policy(session.policy)}"
    )
    await ask_for_data(update, context, session)


async def ask_for_data(update, context, session):
    session.waiting_for = "data"
    await update.effective_chat.send_message(
        "Now paste the DATA block exactly as generated, starting with:\n\n"
        "DATA = {\n"
        "...\n"
        "}\n\n"
        "Then send it as one message."
    )


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = session_for(user_id)
    text = update.message.text or ""

    if session.busy:
        await update.message.reply_text("A job is already running. Please wait for it to finish.")
        return

    if session.waiting_for == "patient_name":
        await handle_patient_search(update, context, session, text)
        return

    if session.waiting_for == "data":
        await handle_data(update, context, session, text)
        return

    await update.message.reply_text(
        "Use /start to begin, or /cancel to reset the current session."
    )


async def handle_patient_search(update, context, session, name):
    if not session.clinic_id:
        await update.message.reply_text("Choose a clinic first with /start.")
        return

    msg = await update.message.reply_text("Checking Smilepass token...")
    session.progress_message_id = msg.message_id

    try:
        if not SMILEPASS_TOKEN:
            await safe_edit(
                context.bot,
                update.effective_chat.id,
                msg.message_id,
                "FAILED\n\nSMILEPASS_TOKEN is not configured on the server.",
            )
            return

        if not check_token(SMILEPASS_TOKEN):
            await safe_edit(
                context.bot,
                update.effective_chat.id,
                msg.message_id,
                "FAILED\n\nThe Smilepass token is invalid or expired.",
            )
            return

        await safe_edit(
            context.bot,
            update.effective_chat.id,
            msg.message_id,
            f"Finding patient:\n{name}\n\nClinic: {CLINICS[session.clinic_id]}",
        )

        matches = search_patient(SMILEPASS_TOKEN, name, session.clinic_id)

        if not matches:
            await safe_edit(
                context.bot,
                update.effective_chat.id,
                msg.message_id,
                f"No patient was returned by Smilepass for:\n{name}\n\n"
                "Check the spelling and send the name again.",
            )
            session.waiting_for = "patient_name"
            return

        candidates = closest_patient_candidates(matches, name)

        if len(candidates) == 1:
            session.patient = candidates[0]
            pname, extra = display_patient(candidates[0])
            text = f"Is this the patient?\n\n{name_text(pname, extra)}"
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Yes", callback_data="patient_confirm"),
                    InlineKeyboardButton("No", callback_data="patient_cancel"),
                ]
            ])
            await safe_edit(
                context.bot,
                update.effective_chat.id,
                msg.message_id,
                text,
                keyboard,
            )
            return

        session.patient_matches = candidates
        session.waiting_for = "patient_choice"

        lines = ["Multiple patients were returned. Choose the correct one:"]
        for i, patient in enumerate(candidates):
            pname, extra = display_patient(patient)
            suffix = f" — {extra[0]}" if extra else ""
            lines.append(f"{i + 1}. {pname}{suffix}")

        await safe_edit(
            context.bot,
            update.effective_chat.id,
            msg.message_id,
            "\n".join(lines),
            patient_keyboard(candidates),
        )

    except requests.HTTPError as exc:
        await safe_edit(
            context.bot,
            update.effective_chat.id,
            msg.message_id,
            f"FAILED\n\nSmilepass request failed:\n{exc}",
        )
    except Exception as exc:
        logger.exception("Patient search failed")
        await safe_edit(
            context.bot,
            update.effective_chat.id,
            msg.message_id,
            f"FAILED\n\nPatient search error:\n{exc}",
        )


def name_text(name, extra):
    text = name
    if extra:
        text += "\n" + "\n".join(extra)
    return text


async def handle_data(update, context, session, raw_text):
    if not session.patient or not session.policy:
        await update.message.reply_text("The patient/policy selection expired. Send /start.")
        return

    try:
        data = parse_data_text(raw_text)
    except Exception as exc:
        await update.message.reply_text(
            f"DATA PARSE FAILED\n\n{exc}\n\n"
            "Please paste the full DATA = {...} block and try again."
        )
        return

    session.busy = True
    session.progress_message_id = None

    try:
        progress_msg = await update.message.reply_text("Starting Smilepass breakdown...")
        session.progress_message_id = progress_msg.message_id

        await progress(update, context, session, "Checking token...")
        if not check_token(SMILEPASS_TOKEN):
            await progress(
                update, context, session,
                "FAILED\n\nSmilepass token is invalid or expired."
            )
            return

        patient = session.patient
        policy = session.policy
        patient_name, _ = display_patient(patient)

        await progress(
            update, context, session,
            f"Finding patient...\n\n{patient_name}"
        )
        # Re-query to stay consistent with the selected clinic/session.
        matches = search_patient(SMILEPASS_TOKEN, patient_name, session.clinic_id)
        if not any(str(p.get("id")) == str(patient.get("id")) for p in matches):
            await progress(
                update, context, session,
                "FAILED\n\nThe selected patient is no longer returned by Smilepass."
            )
            return

        await progress(update, context, session, "Getting insurance policy...")
        policies = get_policies(SMILEPASS_TOKEN, patient["id"])
        if not any(str(p.get("id")) == str(policy.get("id")) for p in policies):
            await progress(
                update, context, session,
                "FAILED\n\nThe selected insurance policy is no longer available."
            )
            return

        await progress(update, context, session, "Saving Fee guide / Deductible / Specialist fee...")
        deductible = data.get("deductible")
        if deductible is None:
            deductible = "0"

        try:
            update_policy_fields(
                SMILEPASS_TOKEN,
                policy["id"],
                data.get("fee_guide"),
                deductible,
                data.get("specialist_fee_covered"),
            )
        except Exception as exc:
            # Same behavior as the desktop app: continue, but report the warning.
            logger.warning("Policy fields failed: %s", exc)

        await progress(update, context, session, "Creating breakdown...")
        breakdown_id = create_breakdown(
            SMILEPASS_TOKEN,
            patient["id"],
            policy["id"],
            session.clinic_id,
        )

        await progress(update, context, session, "Updating plan limits...")
        plan_error = None
        try:
            update_plan_boxes(
                SMILEPASS_TOKEN,
                breakdown_id,
                data.get("plans"),
                session.clinic_id,
            )
        except Exception as exc:
            plan_error = str(exc)
            logger.warning("Plan boxes failed: %s", exc)

        procedures = data.get("procedures", [])

        # Keep first instance of each code, matching the desktop app.
        seen = set()
        unique_procedures = []
        for proc in procedures:
            code = str(proc.get("code", "?"))
            if code not in seen:
                seen.add(code)
                unique_procedures.append(proc)

        await progress(
            update, context, session,
            f"Updating procedures 0/{len(unique_procedures)}..."
        )

        mapping = get_procedure_mapping(SMILEPASS_TOKEN, breakdown_id)

        # Any Smilepass procedure not mentioned in DATA gets "Estimate required".
        mentioned_codes = {str(p.get("code")) for p in unique_procedures}
        leftovers = [
            code for code in mapping.keys()
            if code not in mentioned_codes
        ]
        for code in leftovers:
            unique_procedures.append({
                "code": code,
                "coverage": None,
                "frequency": None,
                "note": "Estimate required",
            })

        total = len(unique_procedures)
        saved = 0
        errors = []

        # Sequential writes make progress reporting deterministic and reduce
        # pressure on the API. The endpoint behavior/payload is the same.
        for proc in unique_procedures:
            code = str(proc.get("code", "?"))
            try:
                save_procedure(
                    SMILEPASS_TOKEN,
                    breakdown_id,
                    mapping,
                    proc,
                    session.clinic_id,
                )
                saved += 1
                await progress(
                    update, context, session,
                    f"Updating procedures {saved}/{total}\n\nLast: {code}"
                )
            except Exception as exc:
                errors.append(f"{code}: {exc}")
                await progress(
                    update, context, session,
                    f"Updating procedures {saved}/{total}\n\n"
                    f"Last: {code} — FAILED"
                )

        if errors or plan_error:
            lines = [
                "FAILED WITH WARNINGS",
                "",
                f"Patient: {patient_name}",
                f"Breakdown ID: {breakdown_id}",
                f"Procedures saved: {saved}/{total}",
            ]
            if plan_error:
                lines += ["", f"Plan boxes: {plan_error}"]
            if errors:
                lines += ["", "Procedure errors:"]
                lines.extend(errors[:20])

            await progress(update, context, session, "\n".join(lines))
        else:
            await progress(
                update, context, session,
                "SUCCESS\n\n"
                f"Patient: {patient_name}\n"
                f"Policy: {display_policy(policy)}\n"
                f"Breakdown ID: {breakdown_id}\n"
                f"Procedures saved: {saved}/{total}\n\n"
                "Workflow complete."
            )

    except requests.HTTPError as exc:
        await progress(
            update, context, session,
            f"FAILED\n\nNetwork/HTTP error:\n{exc}"
        )
    except Exception as exc:
        logger.exception("Breakdown job failed")
        await progress(
            update, context, session,
            f"FAILED\n\nError:\n{exc}"
        )
    finally:
        session.busy = False
        session.waiting_for = None


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data

    if data.startswith("clinic:"):
        await clinic_callback(update, context)
    elif data.startswith("patient:") or data in {"patient_confirm", "patient_cancel"}:
        await patient_callback(update, context)
    elif data.startswith("policy:"):
        await policy_callback(update, context)
    else:
        await update.callback_query.answer("Unknown action.")


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured.")
    if not SMILEPASS_TOKEN:
        raise RuntimeError("SMILEPASS_TOKEN is not configured.")

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CallbackQueryHandler(callback_router))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))

    logger.info("Smilepass Telegram bot starting...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
