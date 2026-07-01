#!/usr/bin/env python
"""
Local Google Forms answer-drafting server.

Tampermonkey calls this server on 127.0.0.1 only. The server can either:
- use an AI provider API key to ask for an answers JSON object, or
- fall back to a deterministic local ruleset from local_data/common_answers.json.

It never submits forms and never stores submitted form content.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.local.json"
LOCAL_DATA_DIR = ROOT / "local_data"
PROFILES_DIR = ROOT / "profiles"
API_DIR = ROOT / "api"
HOST = "127.0.0.1"
PORT = int(os.environ.get("FORMS_AI_PORT", "8799"))
AI_PROVIDER = os.environ.get("AI_PROVIDER", "").strip().lower()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
MANUAL_REQUIRED = "MANUAL_REQUIRED"
DEFAULT_CONFIG = {
    "activeProfile": "erasmus",
    "provider": "auto",
    "privacyMode": "filtered_ai"
}
ALLOWED_PROVIDERS = {"auto", "local_rules", "local", "google", "gemini", "openai"}
ALLOWED_PRIVACY_MODES = {"filtered_ai", "local_only"}
BUILT_IN_DIRECT_QUESTION_MAP = {
    "dni": "DNI",
    "id or passport number": "DNI",
    "passport number": "DNI",
    "id number": "DNI",
    "documento nacional de identidad": "DNI",
    "direccion": "ADDRESS",
    "current address": "FULL_ADDRESS",
    "domicilio": "ADDRESS",
    "codigo postal": "POSTAL_CODE",
    "cp": "POSTAL_CODE",
    "nombre completo": "LEGAL_FULL_NAME",
    "nombre y apellidos": "LEGAL_FULL_NAME",
    "first name": "FIRST_NAME",
    "nombre": "FIRST_NAME",
    "last name": "LAST_NAME",
    "apellidos": "LAST_NAME",
    "fecha de nacimiento": "DATE_OF_BIRTH",
    "correo electronico": "EMAIL",
    "correo electronico personal": "EMAIL",
    "email": "EMAIL",
    "e-mail": "EMAIL",
    "telefono": "PHONE",
    "telefono movil": "PHONE",
    "telefono movil personal": "PHONE",
    "phone": "PHONE",
    "mobile": "PHONE",
    "localidad": "LOCALITY",
    "provincia": "PROVINCE",
    "nacionalidad": "NATIONALITY",
    "municipio y provincia de residencia": "CURRENT_CITY",
    "municipio de residencia": "CURRENT_CITY",
    "provincia de residencia": "CURRENT_CITY",
    "ciudad de residencia": "CURRENT_CITY",
    "ciudad donde vives": "CURRENT_CITY",
    "pais de residencia": "COUNTRY_OF_RESIDENCE",
    "genero": "GENDER",
    "sexo": "GENDER",
    "pronouns": "PRONOUNS",
    "occupation": "OCCUPATION",
    "studies": "STUDIES",
    "estudios finalizados": "COMPLETED_STUDIES",
    "situacion": "CURRENT_SITUATION",
    "edad": "AGE_RANGE"
}


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return load_json(path)


def load_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def profile_paths(profile_id: str) -> dict[str, Path]:
    clean_id = re.sub(r"[^a-zA-Z0-9_-]", "", profile_id or "") or DEFAULT_CONFIG["activeProfile"]
    folder = PROFILES_DIR / clean_id
    return {
        "id": clean_id,
        "aiProfile": folder / "ai_profile.md" if folder.exists() else PROFILES_DIR / f"ai_profile_{clean_id}.md",
        "privateProfile": folder / "private_profile.md" if folder.exists() else PROFILES_DIR / f"profile_{clean_id}.md",
        "commonAnswers": folder / "common_answers.json" if (folder / "common_answers.json").exists() else LOCAL_DATA_DIR / f"common_answers_{clean_id}.json"
    }


def list_profiles() -> list[dict[str, Any]]:
    profile_ids: set[str] = set()
    if PROFILES_DIR.exists():
        for path in PROFILES_DIR.glob("ai_profile_*.md"):
            profile_ids.add(path.stem.replace("ai_profile_", "", 1))
        for path in PROFILES_DIR.iterdir():
            if path.is_dir():
                profile_ids.add(path.name)
    if not profile_ids:
        profile_ids.add(DEFAULT_CONFIG["activeProfile"])

    profiles = []
    for profile_id in sorted(profile_ids):
        paths = profile_paths(profile_id)
        profiles.append(
            {
                "id": profile_id,
                "label": profile_id.replace("_", " ").title(),
                "hasAiProfile": paths["aiProfile"].exists(),
                "hasPrivateProfile": paths["privateProfile"].exists(),
                "hasCommonAnswers": paths["commonAnswers"].exists()
            }
        )
    return profiles


def load_config() -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    saved = load_json_if_exists(CONFIG_PATH)
    if isinstance(saved, dict):
        config.update({key: saved[key] for key in DEFAULT_CONFIG if key in saved})
    return normalize_config(config)


def normalize_config(config: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(DEFAULT_CONFIG)
    if isinstance(config, dict):
        aliases = {
            "profileId": "activeProfile",
            "profile": "activeProfile"
        }
        for key, value in config.items():
            merged[aliases.get(key, key)] = value

    profile_ids = {profile["id"] for profile in list_profiles()}
    if merged["activeProfile"] not in profile_ids:
        merged["activeProfile"] = DEFAULT_CONFIG["activeProfile"] if DEFAULT_CONFIG["activeProfile"] in profile_ids else sorted(profile_ids)[0]

    provider = str(merged.get("provider", "auto")).strip().lower()
    merged["provider"] = provider if provider in ALLOWED_PROVIDERS else "auto"
    if merged["provider"] == "local":
        merged["provider"] = "local_rules"
    if merged["provider"] == "gemini":
        merged["provider"] = "google"

    privacy_mode = str(merged.get("privacyMode", "filtered_ai")).strip().lower()
    merged["privacyMode"] = privacy_mode if privacy_mode in ALLOWED_PRIVACY_MODES else "filtered_ai"
    return merged


def save_config(config: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_config(config)
    CONFIG_PATH.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return normalized


def load_common_answers(profile_id: str) -> dict[str, Any]:
    paths = profile_paths(profile_id)
    if not paths["commonAnswers"].exists():
        return {"profile": {}, "directQuestionMap": {}, "commonAnswers": {}}
    return load_json(paths["commonAnswers"])


def load_ai_profile(profile_id: str) -> str:
    return load_text(profile_paths(profile_id)["aiProfile"])


def load_secret_file(*names: str) -> str:
    for name in names:
        path = API_DIR / name
        if not path.exists() or not path.is_file():
            continue
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    return ""


def normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = re.sub(r"[^\w\s@+.-]", " ", without_accents, flags=re.UNICODE)
    return re.sub(r"\s+", " ", cleaned).strip().lower()


def option_value(question: dict[str, Any], desired: Any) -> Any:
    options = question.get("options") or []
    if not options:
        return desired
    if isinstance(desired, list):
        selected = []
        for item in desired:
            match = option_value(question, item)
            if match != MANUAL_REQUIRED:
                selected.append(match)
        return selected if selected else MANUAL_REQUIRED

    desired_norm = normalize(str(desired))
    option_synonyms = {
        "male": {"male", "hombre", "masculino"},
        "female": {"female", "mujer", "femenino"},
        "yes": {"yes", "si", "sí"},
        "no": {"no"},
        "studies": {"studies", "estudios"},
        "unemployed": {"unemployed", "desempleadx", "desempleado", "desempleada"},
        "university": {"university", "universitarios", "universitario", "universitaria"},
        "social media": {"social media", "redes sociales", "por las redes sociales de europaerestu"}
    }
    for option in options:
        if normalize(str(option)) == desired_norm:
            return option
    for group in option_synonyms.values():
        if desired_norm in group:
            for option in options:
                option_norm = normalize(str(option))
                if option_norm in group:
                    return option
    return MANUAL_REQUIRED


def first_option(question: dict[str, Any]) -> Any:
    options = question.get("options") or []
    return options[0] if len(options) == 1 else MANUAL_REQUIRED


def local_answer_for_question(question: dict[str, Any], config: dict[str, Any]) -> Any:
    title = normalize(question.get("title", ""))
    form_title = normalize(question.get("formTitle", ""))
    profile = config["profile"]
    direct_map = {**BUILT_IN_DIRECT_QUESTION_MAP, **config.get("directQuestionMap", {})}
    common = config["commonAnswers"]

    if "nivel de ingles" in title:
        desired = common.get("english_level_spanish")
        if desired:
            return option_value(question, desired)
        return option_value(question, profile.get("ENGLISH_LEVEL_NUMERIC", profile.get("ENGLISH_LEVEL", MANUAL_REQUIRED)))
    if "level of english" in title:
        return option_value(question, common.get("english_level_english", profile.get("SPOKEN_ENGLISH_LEVEL", MANUAL_REQUIRED)))
    if "do you consider yourself a youth worker" in title:
        return option_value(question, common.get("youth_worker_answer", MANUAL_REQUIRED))
    if "why do you consider yourself as a youth worker" in title:
        return common.get("youth_worker_reason", MANUAL_REQUIRED)
    if "other languages" in title:
        return common.get("other_languages", MANUAL_REQUIRED)

    for needle, profile_key in direct_map.items():
        if normalize(needle) in title:
            return option_value(question, profile.get(profile_key, MANUAL_REQUIRED))

    question_type = normalize(str(question.get("type", "")))
    if question_type == "email" and "emergency" not in title and "emergencia" not in title:
        return option_value(question, profile.get("EMAIL", MANUAL_REQUIRED))
    if question_type == "phone" or "telefono" in title or "tel fono" in title or "movil" in title or "m vil" in title:
        return option_value(question, profile.get("PHONE", MANUAL_REQUIRED))
    if question_type == "date" and ("birth" in title or "nacimiento" in title):
        return option_value(question, profile.get("DATE_OF_BIRTH", MANUAL_REQUIRED))

    if "has participado anteriormente" in title or "participado anteriormente en proyectos internacionales" in title:
        return option_value(question, common.get("international_projects_previous", "Sí"))
    if "cuantos has participado" in title or "cuantos proyectos" in title:
        return option_value(question, common.get("erasmus_plus_project_count_range", common.get("erasmus_plus_project_count", "1")))
    if "alergias" in title or "intolerancias" in title or "dietas" in title or "dieta" in title:
        return common.get("dietary_requirements_spanish", profile.get("DIETARY_MEDICAL_TRAVEL_REQUIREMENTS", MANUAL_REQUIRED))
    if "condiciones de participacion" in title or "condiciones de participación" in title:
        matched = option_value(question, common.get("accept_participation_conditions", first_option(question)))
        return first_option(question) if matched == MANUAL_REQUIRED else matched
    if "tratamiento de datos" in title or "datos personales" in title:
        matched = option_value(question, common.get("accept_data_processing", first_option(question)))
        return first_option(question) if matched == MANUAL_REQUIRED else matched
    if "dificultades economicas" in title or "dificultades económicas" in title or "dificultades sociales" in title or "dificultades culturales" in title or "dificultades geograficas" in title or "dificultades geográficas" in title:
        return common.get("access_difficulties_spanish", MANUAL_REQUIRED)
    if "discapacidad" in title or "necesidad de apoyo" in title:
        return common.get("disability_support_needs_spanish", MANUAL_REQUIRED)
    if "que esperas de este proyecto" in title:
        return common.get("expectations_project_spanish", MANUAL_REQUIRED)
    if "que podrias aportar" in title or "que podrías aportar" in title:
        return common.get("contribution_project_spanish", MANUAL_REQUIRED)
    if "cual es tu motivacion" in title or "cuál es tu motivación" in title:
        return common.get("motivation_project_spanish", MANUAL_REQUIRED)
    if "perteneces a algun colectivo" in title or "perteneces a un colectivo" in title:
        return option_value(question, common.get("fewer_opportunities_spanish", "No"))
    if "explica tu respuesta" in title:
        return MANUAL_REQUIRED
    if "reunion de preseleccion" in title or "sesiones preparatorias" in title:
        return option_value(question, common.get("preselection_meeting_availability", "Sí"))
    if "como has conocido esta actividad" in title or "como conociste esta actividad" in title:
        return option_value(question, common.get("source_europaerestu", common.get("source", MANUAL_REQUIRED)))

    if "participant or as a leader" in title:
        return option_value(question, "Participant (18-25)")
    if "previously participated in erasmus" in title or "youth exchanges or training courses" in title:
        return option_value(question, common["erasmus_experience_count"])
    if "education experience" in title or "school/study" in title:
        return common["education"]
    if "member of any organization" in title:
        return option_value(question, common["member_organization"])
    if "role in the organization" in title:
        return common["organization_role"]
    if "how can you contribute" in title:
        return common["contribution"]
    if "feel called" in title or "meaningful for you now" in title or "roots & wings" in title:
        return common["motivation_nature_inner_growth"]
    if "nature, inner balance" in title or "sustainable ways of living" in title:
        return common["nature_balance_sustainable_living"]
    if "explore, transform, or awaken" in title:
        return common["inner_transformation"]
    if "motivation" in title or "why do you want" in title:
        joined = normalize(question.get("title", "") + " " + question.get("formTitle", ""))
        if "nature" in joined or "roots" in joined or "wings" in joined or "inner" in joined:
            return common["motivation_nature_inner_growth"]
        if "digital" in joined or "citizen" in joined:
            return common["motivation_digital_citizenship"]
        return common["motivation_general"]
    if "experience in erasmus" in title or "european youth projects" in title:
        return common["erasmus_experience"]
    if "qualities" in title or "gifts" in title or "energy" in title:
        return common["group_qualities_nature"]
    if "co-create" in title or "creative workshop" in title or "movement session" in title or "reflection space" in title:
        return common["co_create_activity"]
    if "use the outcomes" in title:
        return common["outcomes"]
    if "learn most" in title:
        joined = normalize(question.get("title", "") + " " + question.get("formTitle", ""))
        if "digital" in joined or "citizen" in joined:
            return common["learn_digital_citizenship"]
        return common["learn_general"]
    if "disseminate" in title:
        return option_value(question, common["dissemination_yes"])
    if "share the experience" in title or "local community" in title:
        return common["dissemination_nature"]
    if "where did you find" in title or "how did you find" in title:
        return common["source"]
    if "fewer opportunities" in title:
        return option_value(question, common["fewer_opportunities"])
    if "facing any situations" in title or "obstacles" in title or "participation in activities more challenging" in title:
        return option_value(question, common["facing_barriers"])
    if "please explain briefly" in title or "what kind of support" in title:
        return common["barriers_explanation"]
    if "hiking" in title or "body movement" in title or "long periods of time in nature" in title:
        return option_value(question, common["nature_physical_comfort"])
    if "participation fee" in title:
        return option_value(question, common["participation_fee"])
    if "future activities" in title:
        return option_value(question, common["future_activities"])
    if "comment" in title:
        return common["final_comment"]
    if "gdpr" in title or "consent" in title:
        return option_value(question, common["gdpr"])

    return MANUAL_REQUIRED if question.get("required") else None


def locked_manual_question(question: dict[str, Any], config: dict[str, Any]) -> bool:
    title = normalize(question.get("title", ""))
    patterns = config.get("manualOnlyQuestionPatterns", [])
    return any(normalize(pattern) in title for pattern in patterns)


def generate_local_answers(extraction: dict[str, Any], run_config: dict[str, Any] | None = None) -> dict[str, Any]:
    runtime = normalize_config(run_config or load_config())
    config = load_common_answers(runtime["activeProfile"])
    answers: dict[str, Any] = {}
    locked_manual_not_sent_to_ai = []
    warnings = [
        "Generated by local deterministic rules only. Review all answers.",
        "Sensitive fields are intentionally MANUAL_REQUIRED."
    ]

    for question in extraction.get("questions", []):
        enriched = dict(question)
        enriched["formTitle"] = extraction.get("formTitle", "")
        answer = local_answer_for_question(enriched, config)
        if answer is not None:
            answers[question["id"]] = answer
            if answer == MANUAL_REQUIRED and locked_manual_question(enriched, config):
                locked_manual_not_sent_to_ai.append(question["id"])

    return {
        "answers": answers,
        "mode": "local_rules",
        "profile": runtime["activeProfile"],
        "warnings": warnings,
        "lockedManualNotSentToAI": locked_manual_not_sent_to_ai,
        "localAnsweredCount": sum(1 for value in answers.values() if is_known_local_answer(value)),
        "lockedManualCount": len(locked_manual_not_sent_to_ai),
        "apiQuestionCount": 0
    }


def extract_json_object(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in model response")
    parsed = json.loads(text[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("Model response JSON is not an object")
    return parsed


def sanitize_extraction_for_ai(extraction: dict[str, Any]) -> dict[str, Any]:
    questions = []
    for question in extraction.get("questions", []):
        questions.append(
            {
                "id": question.get("id"),
                "title": question.get("title"),
                "required": question.get("required"),
                "type": question.get("type"),
                "options": question.get("options") or []
            }
        )
    return {
        "formTitle": extraction.get("formTitle", ""),
        "questions": questions
    }


def sanitize_common_answers_for_ai(config: dict[str, Any]) -> dict[str, Any]:
    allowed_profile_keys = {
        "CURRENT_CITY",
        "ALTERNATIVE_CURRENT_CITY",
        "BIRTH_CITY",
        "DEPARTURE_AIRPORT",
        "AGE_AUGUST_2026",
        "GENDER",
        "ENGLISH_LEVEL",
        "SPOKEN_ENGLISH_LEVEL",
        "COUNTRY",
        "COUNTRY_OF_RESIDENCE",
        "NATIONALITY",
        "CURRENT_OCCUPATION"
    }
    blocked_answer_keys = {
        "gdpr",
        "participation_fee",
        "source_options_reference"
    }
    safe_config = {
        "profile": {
            key: value
            for key, value in config.get("profile", {}).items()
            if key in allowed_profile_keys
        },
        "commonAnswers": {
            key: value
            for key, value in config.get("commonAnswers", {}).items()
            if key not in blocked_answer_keys
        },
        "referenceAnswers": config.get("referenceAnswers", {}),
        "styleRules": config.get("styleRules", [])
    }
    return safe_config


def build_prompt(extraction: dict[str, Any], run_config: dict[str, Any] | None = None) -> dict[str, Any]:
    runtime = normalize_config(run_config or load_config())
    config = load_common_answers(runtime["activeProfile"])
    profile = load_ai_profile(runtime["activeProfile"])
    return {
        "task": "Generate a Google Forms answers JSON object. Keys must be q_id values from the extraction. Values must be truthful draft answers or MANUAL_REQUIRED.",
        "hard_rules": [
            "Return only valid JSON, no markdown.",
            "Questions already answered by local common_answers are not included in this extraction; do not create answers for missing q_ids.",
            "Answer each question in the same language as the question title. If the question or form is Spanish, answer in Spanish.",
            "If profile_context or styleRules mention English, treat that only as a tone reference; the question language still has priority.",
            "Never invent legal surname, email, phone, emergency contact, date of birth, social links, medical/travel requirements, fee consent, or GDPR consent.",
            "Use MANUAL_REQUIRED for any uncertain or sensitive answer.",
            "Match radio/checkbox answers exactly to available options, preserving the option language and spelling.",
            "Do not include keys for duplicated option artifacts that are not real questions."
        ],
        "profile_context": profile,
        "common_answers_json": sanitize_common_answers_for_ai(config),
        "extraction": sanitize_extraction_for_ai(extraction)
    }


def call_openai(extraction: dict[str, Any], run_config: dict[str, Any] | None = None) -> dict[str, Any]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return generate_local_answers(extraction, run_config)

    runtime = normalize_config(run_config or load_config())
    prompt = build_prompt(extraction, runtime)
    payload = {
        "model": OPENAI_MODEL,
        "input": json.dumps(prompt, ensure_ascii=False)
    }

    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        },
        method="POST"
    )

    with urllib.request.urlopen(request, timeout=90) as response:
        raw = json.loads(response.read().decode("utf-8"))

    output_text = raw.get("output_text")
    if not output_text:
        pieces = []
        for item in raw.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"} and content.get("text"):
                    pieces.append(content["text"])
        output_text = "\n".join(pieces)

    answers = extract_json_object(output_text or "")
    return {
        "answers": answers,
        "mode": f"openai:{OPENAI_MODEL}",
        "profile": runtime["activeProfile"],
        "warnings": ["Generated by OpenAI through localhost. Review before filling and submitting."],
        "apiQuestionCount": len(extraction.get("questions", []))
    }


def gemini_api_key() -> str:
    return (
        os.environ.get("GOOGLE_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or load_secret_file("google.txt", "gemini.txt", "google_api_key.txt", "gemini_api_key.txt")
        or ""
    )


def google_key_source() -> str:
    if os.environ.get("GOOGLE_API_KEY"):
        return "GOOGLE_API_KEY"
    if os.environ.get("GEMINI_API_KEY"):
        return "GEMINI_API_KEY"
    if load_secret_file("google.txt", "gemini.txt", "google_api_key.txt", "gemini_api_key.txt"):
        return "api_file"
    return "none"


def call_google(extraction: dict[str, Any], run_config: dict[str, Any] | None = None) -> dict[str, Any]:
    api_key = gemini_api_key()
    if not api_key:
        return generate_local_answers(extraction, run_config)

    runtime = normalize_config(run_config or load_config())
    prompt = build_prompt(extraction, runtime)
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": json.dumps(prompt, ensure_ascii=False)
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.3,
            "responseMimeType": "application/json"
        }
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json"
        },
        method="POST"
    )

    with urllib.request.urlopen(request, timeout=90) as response:
        raw = json.loads(response.read().decode("utf-8"))

    pieces = []
    for candidate in raw.get("candidates", []):
        content = candidate.get("content", {})
        for part in content.get("parts", []):
            if part.get("text"):
                pieces.append(part["text"])
    output_text = "\n".join(pieces)
    answers = extract_json_object(output_text)
    return {
        "answers": answers,
        "mode": f"google:{GEMINI_MODEL}",
        "profile": runtime["activeProfile"],
        "warnings": ["Generated by Google Gemini through localhost. Review before filling and submitting."],
        "apiQuestionCount": len(extraction.get("questions", []))
    }


def resolve_provider(run_config: dict[str, Any] | None = None) -> str:
    runtime = normalize_config(run_config or load_config())
    configured_provider = runtime["provider"]

    if runtime["privacyMode"] == "local_only":
        return "local_rules"

    env_provider = AI_PROVIDER if AI_PROVIDER else ""
    provider = configured_provider if configured_provider != "auto" else env_provider

    if provider in {"local", "local_rules"}:
        return "local_rules"
    if provider in {"google", "gemini"}:
        return "google" if gemini_api_key() else "local_rules"
    if provider == "openai":
        return "openai" if os.environ.get("OPENAI_API_KEY") else "local_rules"
    if gemini_api_key():
        return "google"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return "local_rules"


def generate_answers(extraction: dict[str, Any], run_config: dict[str, Any] | None = None) -> dict[str, Any]:
    runtime = normalize_config(run_config or load_config())
    provider = resolve_provider(runtime)
    local_result = generate_local_answers(extraction, runtime)
    if provider == "google":
        ai_extraction, local_answers = extraction_without_local_known_answers(extraction, local_result)
        if not ai_extraction.get("questions"):
            local_result["warnings"].append("All detected questions were answered locally; no AI provider request was needed.")
            local_result["localOnlyAnswered"] = True
            local_result["localAnswersNotSentToAI"] = list(local_answers.keys())
            local_result["localAnsweredCount"] = len(local_answers)
            return local_result
        try:
            provider_result = call_google(ai_extraction, runtime)
        except urllib.error.HTTPError as error:
            return local_fallback_after_provider_error(local_result, local_answers, ai_extraction, "Google Gemini", error)
        return merge_answer_layers(local_result, provider_result, local_answers)
    if provider == "openai":
        ai_extraction, local_answers = extraction_without_local_known_answers(extraction, local_result)
        if not ai_extraction.get("questions"):
            local_result["warnings"].append("All detected questions were answered locally; no AI provider request was needed.")
            local_result["localOnlyAnswered"] = True
            local_result["localAnswersNotSentToAI"] = list(local_answers.keys())
            local_result["localAnsweredCount"] = len(local_answers)
            return local_result
        try:
            provider_result = call_openai(ai_extraction, runtime)
        except urllib.error.HTTPError as error:
            return local_fallback_after_provider_error(local_result, local_answers, ai_extraction, "OpenAI", error)
        return merge_answer_layers(local_result, provider_result, local_answers)
    return local_result


def local_fallback_after_provider_error(
    local_result: dict[str, Any],
    local_answers: dict[str, Any],
    ai_extraction: dict[str, Any],
    provider_name: str,
    error: urllib.error.HTTPError
) -> dict[str, Any]:
    answers = dict(local_result.get("answers", {}))
    detail = error.read().decode("utf-8", errors="replace")
    warnings = list(local_result.get("warnings", []))
    warnings.append(
        f"{provider_name} returned HTTP {error.code}. Filled local answers only; API questions were left MANUAL_REQUIRED."
    )
    if detail:
        warnings.append(detail[:500])

    api_questions = ai_extraction.get("questions", [])
    for question in api_questions:
        answers[question.get("id")] = MANUAL_REQUIRED

    fallback = dict(local_result)
    fallback["answers"] = answers
    fallback["mode"] = f"local_fallback_after_{provider_name.lower().replace(' ', '_')}_error"
    fallback["warnings"] = warnings
    fallback["providerError"] = {
        "provider": provider_name,
        "status": error.code,
        "message": detail[:1000]
    }
    fallback["localAnswersNotSentToAI"] = list(local_answers.keys())
    fallback["localAnsweredCount"] = len(local_answers)
    fallback["apiQuestionCount"] = len(api_questions)
    fallback["apiQuestionsFailed"] = [
        {
            "id": question.get("id"),
            "title": question.get("title"),
            "type": question.get("type"),
            "required": question.get("required")
        }
        for question in api_questions
    ]
    fallback["aiUnavailableFallback"] = True
    return fallback


def preview_local_routing(extraction: dict[str, Any], run_config: dict[str, Any] | None = None) -> dict[str, Any]:
    runtime = normalize_config(run_config or load_config())
    local_result = generate_local_answers(extraction, runtime)
    ai_extraction, local_answers = extraction_without_local_known_answers(extraction, local_result)
    locked_manual = local_result.get("lockedManualNotSentToAI", [])
    return {
        "mode": resolve_provider(runtime),
        "profile": runtime["activeProfile"],
        "totalQuestionCount": len(extraction.get("questions", [])),
        "localAnsweredCount": len(local_answers),
        "localAnswersNotSentToAI": list(local_answers.keys()),
        "lockedManualCount": len(locked_manual),
        "lockedManualNotSentToAI": locked_manual,
        "apiQuestionCount": len(ai_extraction.get("questions", [])),
        "apiQuestionIds": [question.get("id") for question in ai_extraction.get("questions", [])],
        "apiQuestions": [
            {
                "id": question.get("id"),
                "title": question.get("title"),
                "type": question.get("type"),
                "required": question.get("required")
            }
            for question in ai_extraction.get("questions", [])
        ]
    }


def is_known_local_answer(value: Any) -> bool:
    if value is None or value == MANUAL_REQUIRED:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    if isinstance(value, list) and not value:
        return False
    return True


def extraction_without_local_known_answers(
    extraction: dict[str, Any],
    local_result: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    local_answers = {
        question_id: answer
        for question_id, answer in local_result.get("answers", {}).items()
        if is_known_local_answer(answer)
    }
    locked_manual = set(local_result.get("lockedManualNotSentToAI", []))
    filtered_questions = [
        question
        for question in extraction.get("questions", [])
        if question.get("id") not in local_answers and question.get("id") not in locked_manual
    ]
    filtered = dict(extraction)
    filtered["questions"] = filtered_questions
    return filtered, local_answers


def merge_answer_layers(
    local_result: dict[str, Any],
    provider_result: dict[str, Any],
    local_answers: dict[str, Any]
) -> dict[str, Any]:
    answers = dict(provider_result.get("answers", {}))
    local_first = []
    locked_manual = list(local_result.get("lockedManualNotSentToAI", []))

    for question_id, local_value in local_answers.items():
        answers[question_id] = local_value
        local_first.append(question_id)
    for question_id in locked_manual:
        answers[question_id] = MANUAL_REQUIRED

    warnings = list(provider_result.get("warnings", []))
    if local_first:
        warnings.append(
            "Questions already answered from local common_answers were not sent to the AI provider: "
            + ", ".join(local_first)
        )
    if locked_manual:
        warnings.append(
            "Questions locked as MANUAL_REQUIRED by local rules were not sent to the AI provider: "
            + ", ".join(locked_manual)
        )

    merged = dict(provider_result)
    merged["answers"] = answers
    merged["localAnswersNotSentToAI"] = local_first
    merged["lockedManualNotSentToAI"] = locked_manual
    merged["localAnsweredCount"] = len(local_first)
    merged["lockedManualCount"] = len(locked_manual)
    merged["apiQuestionCount"] = provider_result.get("apiQuestionCount", 0)
    merged["warnings"] = warnings
    return merged


def health_payload(run_config: dict[str, Any] | None = None) -> dict[str, Any]:
    runtime = normalize_config(run_config or load_config())
    return {
        "ok": True,
        "mode": resolve_provider(runtime),
        "config": runtime,
        "profiles": list_profiles(),
        "configuredProvider": runtime["provider"],
        "envProvider": AI_PROVIDER or "auto",
        "openaiKey": bool(os.environ.get("OPENAI_API_KEY")),
        "googleKey": bool(gemini_api_key()),
        "googleKeySource": google_key_source()
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "FormsLocalAIServer/0.1"

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "https://docs.google.com")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/health":
            self.write_json(200, health_payload())
            return
        if self.path == "/config":
            self.write_json(200, health_payload())
            return
        self.write_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/config":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                config = save_config(payload.get("config", payload))
                self.write_json(200, health_payload(config))
            except Exception as error:
                self.write_json(400, {"error": str(error)})
            return

        if self.path not in {"/generate-answers", "/preview-local"}:
            self.write_json(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            extraction = payload.get("extraction")
            if not isinstance(extraction, dict):
                raise ValueError("Missing extraction object")
            runtime_config = normalize_config(payload.get("config") or load_config())
            if self.path == "/preview-local":
                result = preview_local_routing(extraction, runtime_config)
            else:
                result = generate_answers(extraction, runtime_config)
            self.write_json(200, result)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            self.write_json(502, {"error": f"AI provider API error: {error.code} {detail}"})
        except Exception as error:
            self.write_json(400, {"error": str(error)})

    def write_json(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    mode = resolve_provider()
    print(f"Forms local AI server listening on http://{HOST}:{PORT} ({mode})")
    print("Health: http://127.0.0.1:%s/health" % PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
