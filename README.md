# AgenticForms

Local Google Forms assistant. It drafts answers locally or through a localhost AI server, fills known fields, marks unknown fields for manual review, and never submits the form.

## Flow

```text
Google Form
-> Tampermonkey userscript
-> http://127.0.0.1:8799
-> local rules or filtered Gemini/OpenAI prompt
-> userscript fills the form
-> you review and submit manually
```

## Start

```powershell
cd "path\to\AgenticForms"
python local_forms_ai_server.py
```

Status:

```text
http://127.0.0.1:8799/health
```

Tampermonkey update link:

```text
http://127.0.0.1:8792/tampermonkey-google-forms-copilot.user.js
```

## Browser UI

The floating icon has three actions:

- `Fill form`: extracts visible questions, asks localhost for answers using the selected config, fills confident answers, and marks manual/unresolved fields.
- `Open panel`: choose profile, answer engine, privacy mode, and check localhost status.
- `Clear`: clears visual marks and local temporary text.

## Files

```text
tampermonkey-google-forms-copilot.user.js
```
Browser userscript. Update Tampermonkey only when this file changes.

```text
local_forms_ai_server.py
```
Local server. It owns profile selection, provider selection, privacy filtering, and answer generation.

```text
config.local.json
```
Private active config saved by the server. Ignored by Git. Template: `config.example.json`.

```text
local_data/common_answers_erasmus.json
```
Private structured autofill memory. Local rules use it directly. Gemini/OpenAI only receive a filtered version. Ignored by Git.

```text
profiles/ai_profile_erasmus.md
```
Private local AI writing profile. The server can send this content to Gemini/OpenAI, so keep it intentional. Ignored by Git.

Template:

```text
profiles/profile.example.md
```

```text
api/google.txt
```
Private Gemini API key. Ignored by Git.

```text
../profile_context/profile_erasmus.md
```
Optional private human reference, outside this repo. It is not read by the server, not sent to AI, and not needed for filling.

## Modes

- `local_rules`: no external network call from the server; only `common_answers_erasmus.json` and deterministic matching.
- `filtered_ai`: sends only form questions, options, the public/minimal AI profile, and filtered common answer context to the selected provider.
- `local_only`: forces local rules even if an API key exists.

If no API key is available, `auto` falls back to `local_rules`.

## Privacy

Gemini/OpenAI do not receive cookies, browser session, form HTML, submit actions, or the form URL. The server strips the URL and sends only extracted question metadata.

Questions already answered from `common_answers_erasmus.json` are removed before the AI request. This keeps private/local answers on your machine and reduces provider token/API usage on long forms.

Sensitive direct fields such as phone, email, date of birth, social links, emergency contact, GDPR consent, and fee consent should stay in `common_answers_erasmus.json` for local filling. Only put context in `ai_profile_erasmus.md` if you are comfortable sending it to the selected AI provider.

Final submit is always manual.

## License

MIT is a good fit for this repo because it is simple, permissive, and matches the style of the original userscript license. It lets you keep the project public, reuse it anywhere, and accept contributions without complex license rules.

Keep the `LICENSE` file unless you want the repo to be private-only and not reusable by others.
