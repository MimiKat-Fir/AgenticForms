#!/usr/bin/env python
"""
Formizzy local Google Forms answer-drafting server.

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


class ProviderResponseError(ValueError):
    """A provider responded successfully but did not produce usable answer JSON."""
DEFAULT_CONFIG = {
    "activeProfile": "erasmus",
    "provider": "auto",
    "privacyMode": "filtered_ai"
}
ALLOWED_PROVIDERS = {"auto", "local_rules", "local", "google", "gemini", "openai"}
ALLOWED_PRIVACY_MODES = {"filtered_ai", "local_only"}
BUILT_IN_DIRECT_QUESTION_MAP = {
    "dni": "DNI",
    "dni nie": "DNI",
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
    "surname": "LAST_NAME",
    "surname s": "LAST_NAME",
    "family name": "LAST_NAME",
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
        "yes": {"yes", "si", "sí", "agree", "i agree", "accept", "i accept", "i consent"},
        "no": {"no"},
        "studies": {"studies", "estudios"},
        "unemployed": {"unemployed", "desempleadx", "desempleado", "desempleada"},
        "university": {"university", "universitarios", "universitario", "universitaria"},
        "social media": {"social media", "redes sociales", "por las redes sociales de europaerestu"},
        "instagram": {"instagram", "instagram post", "post de instagram"}
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


def preferred_option(question: dict[str, Any], aliases: set[str]) -> Any:
    for option in question.get("options") or []:
        option_norm = normalize(str(option))
        if option_norm in aliases:
            return option
    return MANUAL_REQUIRED


def erasmus_project_count_option(question: dict[str, Any], count: int) -> Any:
    """Pick an option whose stated numeric range includes the known project count."""
    options = question.get("options") or []
    if not options:
        return str(count)
    for option in options:
        option_norm = normalize(str(option))
        numbers = [int(value) for value in re.findall(r"\d+", option_norm)]
        if len(numbers) == 1 and numbers[0] == count:
            return option
        if len(numbers) >= 2 and min(numbers) <= count <= max(numbers):
            return option
        if count == 1 and option_norm in {"uno", "one", "un proyecto", "one project"}:
            return option
    return MANUAL_REQUIRED


def english_level_option(question: dict[str, Any], common: dict[str, Any], profile: dict[str, Any]) -> Any:
    desired_values = [
        common.get("english_level_spanish"),
        common.get("english_level_english"),
        profile.get("ENGLISH_LEVEL"),
        common.get("english_level_numeric"),
        profile.get("ENGLISH_LEVEL_NUMERIC")
    ]
    for desired in desired_values:
        if desired:
            matched = option_value(question, desired)
            if matched != MANUAL_REQUIRED:
                return matched
    return preferred_option(question, {"avanzado", "advanced", "alto", "high", "c1", "5"})


def dietary_answer(title: str, common: dict[str, Any], profile: dict[str, Any]) -> Any:
    english_markers = (
        "allergies", "food preferences", "special needs", "special considerations",
        "dietary requirements", "dietary restrictions", "dietary needs", "food allergy", "medical concerns",
        "chronic conditions", "reduced mobility"
    )
    spanish_markers = ("alergias", "intolerancias", "dietas", "dieta", "necesidades personales")
    if any(marker in title for marker in english_markers):
        return common.get("dietary_requirements_english", profile.get("DIETARY_MEDICAL_TRAVEL_REQUIREMENTS", MANUAL_REQUIRED))
    if any(marker in title for marker in spanish_markers):
        return common.get("dietary_requirements_spanish", profile.get("DIETARY_MEDICAL_TRAVEL_REQUIREMENTS", MANUAL_REQUIRED))
    return common.get("dietary_requirements_english", profile.get("DIETARY_MEDICAL_TRAVEL_REQUIREMENTS", MANUAL_REQUIRED))


def direct_question_matches(alias: str, title: str) -> bool:
    normalized_alias = normalize(alias)
    if not normalized_alias:
        return False
    if title == normalized_alias:
        return True

    # One-word aliases are too broad for substring matching. Example:
    # "situacion" must not match "situacion personal o social...".
    if len(normalized_alias.split()) == 1:
        return False

    return normalized_alias in title


def local_answer_for_question(question: dict[str, Any], config: dict[str, Any]) -> Any:
    title = normalize(question.get("title", ""))
    form_title = normalize(question.get("formTitle", ""))
    profile = config["profile"]
    direct_map = {**BUILT_IN_DIRECT_QUESTION_MAP, **config.get("directQuestionMap", {})}
    common = config["commonAnswers"]

    if "nivel de ingles" in title:
        return english_level_option(question, common, profile)
    if "level of english" in title or "english level" in title:
        return english_level_option(question, common, profile)
    if title in {"name", "name s", "first name"}:
        return option_value(question, profile.get("FIRST_NAME", MANUAL_REQUIRED))
    if "name and surname" in title:
        return option_value(question, profile.get("LEGAL_FULL_NAME", MANUAL_REQUIRED))
    if "name you want to be called" in title or "name to be called during" in title:
        return option_value(question, profile.get("PREFERRED_PROJECT_NAME", MANUAL_REQUIRED))
    if title == "genero" or title == "sexo":
        return option_value(question, common.get("gender_spanish", profile.get("GENDER", MANUAL_REQUIRED)))
    if title == "gender":
        return option_value(question, profile.get("GENDER", MANUAL_REQUIRED))
    if "do you consider yourself a youth worker" in title:
        return option_value(question, common.get("youth_worker_answer", MANUAL_REQUIRED))
    if "why do you consider yourself as a youth worker" in title:
        return common.get("youth_worker_reason", MANUAL_REQUIRED)
    if "other languages" in title:
        return common.get("other_languages", MANUAL_REQUIRED)

    # These are recurring Erasmus application questions where a local profile has
    # a stable answer. Keep them before the generic direct-map matching.
    if "instagram" in title:
        return option_value(question, profile.get("INSTAGRAM_PROFILE", profile.get("INSTAGRAM_USERNAME", MANUAL_REQUIRED)))
    if "facebook" in title:
        return option_value(question, profile.get("SOCIAL_PROFILE", MANUAL_REQUIRED))
    if "social media account" in title or "social media profile" in title:
        return option_value(question, profile.get("INSTAGRAM_PROFILE", MANUAL_REQUIRED))
    if "lugar de residencia" in title or "place of residence" in title:
        return option_value(question, profile.get("COUNTRY_OF_RESIDENCE", MANUAL_REQUIRED))
    if "ciudad pueblo de residencia" in title or "city town of residence" in title:
        return option_value(question, profile.get("CURRENT_CITY", MANUAL_REQUIRED))
    if "city where you live" in title or "city you live in" in title:
        return option_value(question, profile.get("CURRENT_CITY", MANUAL_REQUIRED))
    if "address street and city" in title:
        return option_value(question, profile.get("ADDRESS", MANUAL_REQUIRED))
    if title == "country":
        return option_value(question, profile.get("COUNTRY_OF_RESIDENCE", MANUAL_REQUIRED))
    if "languages you speak" in title:
        return common.get("other_languages", MANUAL_REQUIRED)
    if "medical insurance valid" in title or "european health insurance card" in title or "ehic" in title:
        return option_value(question, profile.get("EU_HEALTH_INSURANCE", "Yes"))
    if "spotify" in title and ("link" in title or "song" in title):
        return option_value(question, profile.get("SPOTIFY_LINK", MANUAL_REQUIRED))
    if title.startswith("video") or ("video" in title and ("link" in title or "share" in title or "attachment" in title)):
        return option_value(question, profile.get("VIDEO_LINK", MANUAL_REQUIRED))
    if "whatsapp number is different" in title or "do not use whatsapp" in title:
        return None
    if "start your journey" in title or "nearest main transport station" in title:
        return option_value(question, profile.get("JOURNEY_START_LINK", MANUAL_REQUIRED))
    if "te consideras una persona con menos oportunidades" in title:
        return option_value(question, common.get("fewer_opportunities_spanish", "No"))
    if "dificultad menos oportunidades" in title and ("dejar la respuesta en blanco" in title or "si no aplica" in title):
        return None
    if "intercambio juvenil" in title and ("como fue tu experiencia" in title or "how was your experience" in title):
        return common.get("erasmus_experience_brief_spanish", MANUAL_REQUIRED)
    if "food requirements explanation" in title:
        return None
    if "food requirements" in title:
        return option_value(question, "No pork")
    if "physical condition or limitation" in title or "taking medication" in title:
        if question.get("options"):
            return option_value(question, "No")
        return common.get("medical_condition_none_english", "Not really")
    if "medical physical condition explanation" in title:
        return None
    if "medical or special physical condition" in title:
        return option_value(question, "No")
    if "explain your difficulty" in title or "describe any additional requirements" in title:
        return None
    if "experiencia previa" in title and "erasmus" in title:
        return option_value(question, common.get("erasmus_experience_option_spanish", MANUAL_REQUIRED))
    if "experiencia" in title and "proyectos europeos" in title:
        return option_value(question, common.get("international_projects_previous", "Sí"))
    if "cuantos proyectos has participado" in title or "en cuantos proyectos has participado" in title:
        return erasmus_project_count_option(question, int(common.get("erasmus_plus_project_count", 1)))
    if "how many erasmus" in title or "how many erasmus+" in title:
        return erasmus_project_count_option(question, int(common.get("erasmus_plus_project_count", 1)))
    if "dispuesto a ser lider" in title or "dispuesto a ser líder" in title or "willing to be a leader" in title or "persona lider del grupo" in title or "persona líder del grupo" in title:
        return option_value(question, common.get("leader_willingness", "No"))
    if "actividad de bregal" in title:
        return option_value(question, "No")
    if title.startswith("si si cuentanos") or title.startswith("if yes tell us"):
        return None
    if "deberiamos elegirte" in title or "deberíamos elegirte" in title or "why should we choose you" in title:
        return common.get("selection_reason_spanish", MANUAL_REQUIRED)
    if "hobbies" in title or "habilidades chulas" in title or "skills you can share" in title:
        if any(marker in title for marker in ("hobbies", "habilidades", "pueden ser utiles", "pueden ser útiles")):
            return common.get("hobbies_skills_spanish", MANUAL_REQUIRED)
        return common.get("hobbies_skills_english", MANUAL_REQUIRED)
    if "t shirt size" in title or "t-shirt size" in title:
        return option_value(question, common.get("t_shirt_size", MANUAL_REQUIRED))
    if "what type of diet" in title or "type of diet do you follow" in title:
        return option_value(question, common.get("diet_type", MANUAL_REQUIRED))
    if "passions and interests" in title:
        return common.get("passions_interests_keywords", MANUAL_REQUIRED)
    if "strengths or hidden talents" in title or "what are you good at" in title:
        return common.get("strengths_hidden_talents_english", MANUAL_REQUIRED)
    if "current level of education" in title or "job position" in title:
        return profile.get("STUDIES", MANUAL_REQUIRED)
    if "requisitos para participar" in title:
        options = question.get("options") or []
        return list(options) if options else MANUAL_REQUIRED
    if "contacto de emergencia" in title or "persona de contacto" in title or "emergency contact" in title or ("contact person" in title and "emergency" in title):
        if ("nombre" in title or "name" in title) and ("telefono" in title or "tel fono" in title or "phone" in title):
            name = profile.get("EMERGENCY_CONTACT_NAME", "")
            phone = profile.get("EMERGENCY_CONTACT_PHONE", "")
            return ", ".join(part for part in (name, phone) if part) or MANUAL_REQUIRED
        if "nombre" in title or "name" in title:
            return option_value(question, profile.get("EMERGENCY_CONTACT_NAME", MANUAL_REQUIRED))
        if "telefono" in title or "tel fono" in title or "phone" in title:
            return option_value(question, profile.get("EMERGENCY_CONTACT_PHONE", MANUAL_REQUIRED))
        if "correo" in title or "email" in title or "e-mail" in title:
            return option_value(question, profile.get("EMERGENCY_CONTACT_EMAIL", MANUAL_REQUIRED))
        return profile.get("EMERGENCY_CONTACT", MANUAL_REQUIRED)

    for needle, profile_key in direct_map.items():
        if direct_question_matches(needle, title):
            return option_value(question, profile.get(profile_key, MANUAL_REQUIRED))

    if "has participado anteriormente" in title or "participado anteriormente en proyectos internacionales" in title:
        return option_value(question, common.get("international_projects_previous", "Sí"))
    if "cuantos has participado" in title or "cuantos proyectos" in title:
        return erasmus_project_count_option(question, int(common.get("erasmus_plus_project_count", 1)))
    if "dietary needs" in title and question.get("options"):
        return option_value(question, common.get("diet_type", MANUAL_REQUIRED))
    if any(marker in title for marker in (
        "alergias", "intolerancias", "dietas", "dieta", "necesidades personales",
        "allergies", "food preferences", "special needs", "special considerations",
        "dietary requirements", "dietary restrictions", "food allergy", "medical concerns",
        "chronic conditions", "reduced mobility"
    )):
        return dietary_answer(title, common, profile)
    if any(marker in title for marker in (
        "info pack", "terms and conditions", "authenticity of the data", "full availability",
        "commit myself to participate", "attend dissemination activities", "personal data that i provided",
        "general protection data regulation", "permission to the coordinator", "use images of me"
    )):
        return option_value(question, "Yes")
    if "condiciones de participacion" in title or "condiciones de participación" in title:
        matched = option_value(question, common.get("accept_participation_conditions", first_option(question)))
        return first_option(question) if matched == MANUAL_REQUIRED else matched
    if "tratamiento de datos" in title or "datos personales" in title or "reglamento general de proteccion de datos" in title or "rgpd" in title:
        matched = option_value(question, common.get("accept_data_processing", first_option(question)))
        return first_option(question) if matched == MANUAL_REQUIRED else matched
    if title == "consent" and question.get("type") == "checkbox":
        options = question.get("options") or []
        return list(options) if options else MANUAL_REQUIRED
    if "accessibility learning or participation needs" in title:
        return option_value(question, "No")
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
    if "como has encontrado esta oportunidad" in title or "how did you find this opportunity" in title:
        return option_value(question, common.get("source", MANUAL_REQUIRED))
    if "from where did you find out" in title or "where did you find out about this project" in title:
        return option_value(question, common.get("source", MANUAL_REQUIRED))

    question_type = normalize(str(question.get("type", "")))
    if question_type == "email" and "emergency" not in title and "emergencia" not in title:
        return option_value(question, profile.get("EMAIL", MANUAL_REQUIRED))
    # Do not trust a Google Forms phone input type by itself. Some regular short
    # answers are rendered as tel inputs, so a clear phone label is required.
    if "telefono" in title or "tel fono" in title or "movil" in title or "m vil" in title or "phone" in title or "mobile" in title:
        return option_value(question, profile.get("PHONE", MANUAL_REQUIRED))
    if question_type == "date" and ("birth" in title or "nacimiento" in title):
        return option_value(question, profile.get("DATE_OF_BIRTH", MANUAL_REQUIRED))

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


def answer_response_schema(extraction: dict[str, Any]) -> dict[str, Any]:
    """Create a constrained Gemini schema for exactly the q_ids being routed."""
    question_ids = [
        str(question.get("id"))
        for question in extraction.get("questions", [])
        if question.get("id")
    ]
    answer_value = {
        "anyOf": [
            {"type": "string"},
            {"type": "array", "items": {"type": "string"}}
        ],
        "description": "A truthful answer, MANUAL_REQUIRED, or an array of exact checkbox options."
    }
    return {
        "type": "object",
        "properties": {question_id: answer_value for question_id in question_ids},
        "required": question_ids,
        "propertyOrdering": question_ids
    }


def provider_response_detail(raw: dict[str, Any]) -> str:
    feedback = raw.get("promptFeedback") or {}
    block_reason = feedback.get("blockReason")
    candidates = raw.get("candidates") or []
    finish_reasons = [candidate.get("finishReason") for candidate in candidates if candidate.get("finishReason")]
    details = []
    if block_reason:
        details.append(f"prompt blocked: {block_reason}")
    if finish_reasons:
        details.append("finish reason: " + ", ".join(finish_reasons))
    return "; ".join(details) or "no candidate text returned"


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
    # The API only needs general drafting context. Direct identifiers and local
    # form answers stay in the browser/local rules and are never forwarded.
    allowed_answer_keys = {
        "education",
        "motivation_general",
        "motivation_digital_citizenship",
        "expectations_project_spanish",
        "contribution_project_spanish",
        "motivation_project_spanish",
        "motivation_nature_inner_growth",
        "nature_balance_sustainable_living",
        "inner_transformation",
        "erasmus_experience",
        "erasmus_experience_brief_spanish",
        "erasmus_experience_count",
        "erasmus_plus_project_count",
        "erasmus_plus_project_count_range",
        "youth_worker_answer",
        "youth_worker_reason",
        "other_languages",
        "contribution",
        "group_qualities_nature",
        "co_create_activity",
        "outcomes",
        "learn_digital_citizenship",
        "learn_general",
        "dissemination_nature",
        "digital_wellbeing_personal",
        "ai_practical_sharing",
        "hobbies_skills_spanish",
        "hobbies_skills_english",
        "health_insurance_ehic"
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
            if key in allowed_answer_keys
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
        "task": "Draft truthful answers for the supplied Google Forms questions.",
        "output_contract": {
            "format": "Return only one valid JSON object. Each key is a supplied q_id and each value is an answer or MANUAL_REQUIRED.",
            "scope": "Answer only supplied q_ids. Questions already answered locally are intentionally absent and must not be recreated.",
            "choice_fields": "For radio and checkbox fields, return only exact available option text. For multiple checkboxes, return an array of exact option text."
        },
        "safety": [
            "Use MANUAL_REQUIRED when an answer is uncertain, sensitive, or would require inventing a fact.",
            "Never invent identifiers, contact details, dates of birth, social links, medical/travel details, legal consent, fee consent, or GDPR consent.",
            "Do not answer duplicate option artifacts that are not real questions."
        ],
        "writing_style": [
            "Write in the same language as each question; Spanish question means Spanish answer.",
            "Use simple, direct, natural wording from a normal 25-year-old applicant.",
            "Prefer one concise sentence. Use two only when necessary.",
            "Only write longer text when the question explicitly states a minimum length; satisfy it with the shortest honest answer.",
            "Avoid corporate, essay-like, motivational-speaker, or obviously AI-written language."
        ],
        "candidate_context": profile,
        "reusable_answer_context": sanitize_common_answers_for_ai(config),
        "questions_to_answer": sanitize_extraction_for_ai(extraction)
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
            "responseMimeType": "application/json",
            "responseSchema": answer_response_schema(extraction)
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
    if not output_text.strip():
        raise ProviderResponseError(f"Google Gemini returned no usable JSON ({provider_response_detail(raw)}).")
    try:
        answers = extract_json_object(output_text)
    except ValueError as error:
        raise ProviderResponseError(f"Google Gemini returned invalid JSON: {error}") from error
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
        except ProviderResponseError as error:
            return local_fallback_after_provider_response_error(local_result, local_answers, ai_extraction, "Google Gemini", str(error))
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
        except ProviderResponseError as error:
            return local_fallback_after_provider_response_error(local_result, local_answers, ai_extraction, "OpenAI", str(error))
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


def local_fallback_after_provider_response_error(
    local_result: dict[str, Any],
    local_answers: dict[str, Any],
    ai_extraction: dict[str, Any],
    provider_name: str,
    detail: str
) -> dict[str, Any]:
    answers = dict(local_result.get("answers", {}))
    api_questions = ai_extraction.get("questions", [])
    for question in api_questions:
        answers[question.get("id")] = MANUAL_REQUIRED

    fallback = dict(local_result)
    fallback["answers"] = answers
    fallback["mode"] = f"local_fallback_after_{provider_name.lower().replace(' ', '_')}_invalid_response"
    fallback["warnings"] = list(local_result.get("warnings", [])) + [
        f"{provider_name} returned an invalid response. Filled local answers only; API questions need manual review."
    ]
    fallback["providerError"] = {
        "provider": provider_name,
        "status": "invalid_response",
        "message": detail
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
        origin = self.headers.get("Origin", "")
        allowed_origins = {"https://docs.google.com", "https://tally.so"}
        if origin in allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
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
