// ==UserScript==
// @name         Formizzy
// @namespace    https://local-only.google-forms-copilot/
// @version      0.4.19
// @description  Make it easy: local review-first helper to draft and fill Google Forms answers through localhost. Never submits forms.
// @author       MimiKat-Fir
// @homepageURL  https://github.com/MimiKat-Fir/AgenticForms
// @supportURL   https://github.com/MimiKat-Fir/AgenticForms/issues
// @downloadURL  http://127.0.0.1:8792/tampermonkey-google-forms-copilot.user.js
// @updateURL    http://127.0.0.1:8792/tampermonkey-google-forms-copilot.user.js
// @match        https://docs.google.com/forms/*
// @grant        GM_setClipboard
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// @noframes
// @license      MIT
// ==/UserScript==

(function () {
    'use strict';

    const APP_ID = 'gfc-local-copilot';
    const MANUAL_REQUIRED = 'MANUAL_REQUIRED';
    const SCRIPT_VERSION = '0.4.19';
    const LOCAL_BASE_URL = 'http://127.0.0.1:8799';
    const LOCAL_SERVER_URL = `${LOCAL_BASE_URL}/generate-answers`;
    const LOCAL_PREVIEW_URL = `${LOCAL_BASE_URL}/preview-local`;
    const CONFIG_STORAGE_KEY = 'agenticFormsConfig';
    const DEFAULT_CONFIG = {
        activeProfile: 'erasmus',
        provider: 'auto'
    };
    const START_SERVER_COMMAND = 'cd "path\\to\\AgenticForms"\n& "$env:USERPROFILE\\miniconda3\\python.exe" local_forms_ai_server.py';

    const SELECTORS = {
        questionContainers: [
            '.Qr7Oae',
            '[role="listitem"]'
        ],
        title: [
            '.M7eMe',
            '[role="heading"]',
            '.HoXoMd'
        ],
        textInput: 'input[type="text"], input[type="email"], input[type="tel"], input[type="url"], input[type="number"], input:not([type])',
        dateInput: 'input[type="date"]',
        textarea: 'textarea',
        radio: '[role="radio"]',
        checkbox: '[role="checkbox"]',
        dropdownTrigger: '[role="listbox"], [role="combobox"]',
        dropdownOption: '[role="option"]'
    };

    let lastExtraction = null;
    let activeProgress = null;

    function normalizeText(value) {
        return String(value || '')
            .replace(/\s*\*\s*$/g, '')
            .replace(/\u00a0/g, ' ')
            .replace(/\s+/g, ' ')
            .trim();
    }

    function normalizeKey(value) {
        return normalizeText(value).toLowerCase();
    }

    function isVisible(element) {
        if (!element) return false;
        const style = window.getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== 'none' &&
            style.visibility !== 'hidden' &&
            style.opacity !== '0' &&
            rect.width > 0 &&
            rect.height > 0;
    }

    function queryFirst(root, selectors) {
        for (const selector of selectors) {
            const found = root.querySelector(selector);
            if (found && isVisible(found)) return found;
        }
        return null;
    }

    function getVisibleText(element) {
        return normalizeText(element ? element.innerText || element.textContent : '');
    }

    function unique(values) {
        const seen = new Set();
        const result = [];
        for (const value of values) {
            const cleaned = normalizeText(value);
            const key = normalizeKey(cleaned);
            if (cleaned && !seen.has(key) && key !== 'choose') {
                seen.add(key);
                result.push(cleaned);
            }
        }
        return result;
    }

    function getQuestionContainers() {
        const containers = [];
        const seen = new Set();
        const containerSelector = SELECTORS.questionContainers.join(',');
        for (const selector of SELECTORS.questionContainers) {
            document.querySelectorAll(selector).forEach((container) => {
                if (seen.has(container) || !isVisible(container)) return;
                const parentQuestion = container.parentElement ? container.parentElement.closest(containerSelector) : null;
                if (parentQuestion) return;
                if (!container.querySelector('input, textarea, [role="radio"], [role="checkbox"], [role="listbox"], [role="combobox"]')) return;
                const title = extractTitle(container);
                if (!title) return;
                seen.add(container);
                containers.push(container);
            });
        }
        return containers;
    }

    function extractFormTitle() {
        const candidates = [
            document.querySelector('[role="heading"]'),
            document.querySelector('.F9yp7e'),
            document.querySelector('title')
        ];
        for (const candidate of candidates) {
            const text = getVisibleText(candidate) || normalizeText(document.title);
            if (text && !/google forms/i.test(text)) return text;
        }
        return normalizeText(document.title);
    }

    function extractTitle(container) {
        const titleElement = queryFirst(container, SELECTORS.title);
        if (titleElement) return normalizeText(titleElement.textContent);

        const labels = Array.from(container.querySelectorAll('[aria-label], [data-params]'))
            .map((el) => el.getAttribute('aria-label') || el.textContent)
            .filter(Boolean);
        return normalizeText(labels[0] || '');
    }

    function extractRequired(container, title) {
        if (container.querySelector('[aria-required="true"], input[required], textarea[required]')) return true;
        const requiredText = Array.from(container.querySelectorAll('*'))
            .some((el) => normalizeText(el.textContent).toLowerCase() === '* required');
        return requiredText || /\*$/.test(String(title || '').trim());
    }

    function optionText(option) {
        const aria = option.getAttribute('aria-label') || option.getAttribute('data-value') || option.getAttribute('data-answer-value');
        const text = getVisibleText(option);
        return normalizeText(aria || text);
    }

    function extractOptions(container, selector) {
        return unique(Array.from(container.querySelectorAll(selector)).map(optionText));
    }

    function inferInputSubtype(container) {
        const input = container.querySelector('input');
        const title = normalizeKey(extractTitle(container));
        const inputType = input ? String(input.type || '').toLowerCase() : '';
        const autocomplete = input ? String(input.autocomplete || '').toLowerCase() : '';
        const combined = `${title} ${inputType} ${autocomplete}`;

        if (inputType === 'email' || /\b(e-mail|email|mail)\b/.test(combined)) return 'email';
        if (inputType === 'tel' || /\b(phone|mobile|tel|telephone|whatsapp)\b/.test(combined)) return 'phone';
        if (inputType === 'date' || /\b(date|birth|dob)\b/.test(combined)) return 'date';
        return 'short_text';
    }

    function detectType(container) {
        if (container.querySelector(SELECTORS.textarea)) return 'paragraph';
        if (container.querySelector(SELECTORS.dateInput)) return 'date';
        if (container.querySelector(SELECTORS.radio)) return 'radio';
        if (container.querySelector(SELECTORS.checkbox)) return 'checkbox';
        if (container.querySelector(SELECTORS.dropdownTrigger)) return 'dropdown';
        if (container.querySelector('input')) return inferInputSubtype(container);
        if (/file upload/i.test(getVisibleText(container))) return 'file_upload';
        return 'unknown';
    }

    function extractQuestions() {
        const containers = getQuestionContainers();
        const questions = containers.map((container, index) => {
            const title = extractTitle(container);
            const type = detectType(container);
            let options = [];

            if (type === 'radio') options = extractOptions(container, SELECTORS.radio);
            if (type === 'checkbox') options = extractOptions(container, SELECTORS.checkbox);
            if (type === 'dropdown') options = extractOptions(container, SELECTORS.dropdownOption);

            return {
                id: `q_${String(index + 1).padStart(3, '0')}`,
                title,
                required: extractRequired(container, title),
                type,
                options,
                status: 'detected'
            };
        });

        lastExtraction = {
            formTitle: extractFormTitle(),
            url: location.href,
            extractedAt: new Date().toISOString(),
            questions
        };

        setTextareaValue('questions', JSON.stringify(lastExtraction, null, 2));
        copyText(JSON.stringify(lastExtraction, null, 2));
        setStatus(`Extracted ${questions.length} visible question(s). JSON was copied if clipboard permission is available.`, 'ok');
        renderReport({ extracted: questions.length });
        return lastExtraction;
    }

    function getLocalConfig() {
        try {
            return { ...DEFAULT_CONFIG, ...(JSON.parse(localStorage.getItem(CONFIG_STORAGE_KEY) || '{}') || {}) };
        } catch (_) {
            return { ...DEFAULT_CONFIG };
        }
    }

    function setLocalConfig(config) {
        const merged = { ...DEFAULT_CONFIG, ...(config || {}) };
        localStorage.setItem(CONFIG_STORAGE_KEY, JSON.stringify(merged));
        return merged;
    }

    function localRequestJson(url, payload, method = 'POST') {
        return new Promise((resolve, reject) => {
            if (typeof GM_xmlhttpRequest !== 'function') {
                reject(new Error('GM_xmlhttpRequest is unavailable. Check Tampermonkey grants.'));
                return;
            }

            const request = {
                method,
                url,
                headers: method === 'POST' ? { 'Content-Type': 'application/json' } : {},
                timeout: 120000,
                onload: (response) => {
                    try {
                        const body = JSON.parse(response.responseText || '{}');
                        if (response.status < 200 || response.status >= 300) {
                            reject(new Error(body.error || `Local server returned HTTP ${response.status}`));
                            return;
                        }
                        resolve(body);
                    } catch (error) {
                        reject(new Error(`Local server returned invalid JSON: ${error.message}`));
                    }
                },
                onerror: () => reject(new Error(`Could not reach local server.\n\nStart it in PowerShell:\n${START_SERVER_COMMAND}`)),
                ontimeout: () => reject(new Error('Local server timed out.'))
            };
            if (method === 'POST') request.data = JSON.stringify(payload || {});
            GM_xmlhttpRequest(request);
        });
    }

    async function generateAnswersViaLocalhost() {
        const extraction = lastExtraction || extractQuestions();
        if (!extraction.questions.length) {
            setStatus('No questions found. Refresh the form and try again.', 'warn');
            return;
        }

        setStatus('Requesting draft answers from localhost...', 'info');
        try {
            const response = await localRequestJson(LOCAL_SERVER_URL, { extraction, config: getLocalConfig() });
            if (!response.answers || typeof response.answers !== 'object' || Array.isArray(response.answers)) {
                throw new Error('Local server response did not include an answers object.');
            }

            setTextareaValue('answers', JSON.stringify(response.answers, null, 2));
            renderReport({
                localhostMode: response.mode || 'unknown',
                warnings: response.warnings || [],
                manualRequired: Object.entries(response.answers)
                    .filter((entry) => entry[1] === MANUAL_REQUIRED)
                    .map((entry) => entry[0])
            });
            setStatus(`Draft answers loaded from localhost (${response.mode || 'unknown'}). Review before Fill Form.`, 'ok');
            openPanel();
        } catch (error) {
            setStatus(error.message, 'error');
            openPanel();
        }
    }

    async function fillFormAutomatically(overrides = {}) {
        showProgress('Filling Google Form...', 'Extracting visible questions...', 8);
        const extraction = extractQuestions();
        if (!extraction.questions.length) {
            finishProgress('No questions found.', 'warn');
            setStatus('No questions found. Refresh the form and try again.', 'warn');
            openPanel();
            return;
        }

        updateProgress('Filling Google Form...', `Extracted ${extraction.questions.length} question(s). Checking local answers...`, 22);
        setStatus('Extracted questions. Drafting answers through localhost...', 'info');
        try {
            const requestConfig = { ...getLocalConfig(), ...(overrides.config || {}) };
            const preview = await localRequestJson(LOCAL_PREVIEW_URL, { extraction, config: requestConfig });
            const localCount = Number(preview.localAnsweredCount) || 0;
            const apiCount = Number(preview.apiQuestionCount) || 0;
            const lockedCount = Number(preview.lockedManualCount) || 0;
            const localPart = `${localCount} answered locally`;
            const lockedPart = lockedCount ? `, ${lockedCount} locked for manual review` : '';
            const apiPart = apiCount ? `, ${apiCount} sent to API. Waiting for API...` : ', no API call needed';
            updateProgress('Filling Google Form...', `${localPart}${lockedPart}${apiPart}`, apiCount ? 38 : 46);

            const response = await localRequestJson(LOCAL_SERVER_URL, { extraction, config: requestConfig });
            if (!response.answers || typeof response.answers !== 'object' || Array.isArray(response.answers)) {
                throw new Error('Local server response did not include an answers object.');
            }

            const responseLocalCount = Number(response.localAnsweredCount) || localCount;
            const responseApiCount = Number(response.apiQuestionCount) || apiCount;
            const responseLockedCount = Number(response.lockedManualCount) || lockedCount;
            const providerIssue = providerIssueMessage(response);
            if (providerIssue) {
                updateProgress('Filling Google Form...', providerIssue, 50);
                setStatus(providerIssue, 'warn');
            }
            updateProgress(
                'Filling Google Form...',
                providerIssue || `Answers ready: ${responseLocalCount} local, ${responseApiCount} API, ${responseLockedCount} manual locked. Filling fields...`,
                58
            );
            setTextareaValue('answers', JSON.stringify(response.answers, null, 2));
            await fillFormFromAnswers({
                progress: true,
                routing: {
                    mode: response.mode || preview.mode || 'unknown',
                    totalQuestionCount: Number(preview.totalQuestionCount) || extraction.questions.length,
                    localAnsweredCount: responseLocalCount,
                    lockedManualCount: responseLockedCount,
                    apiQuestionCount: responseApiCount,
                    apiQuestions: preview.apiQuestions || [],
                    apiQuestionsFailed: response.apiQuestionsFailed || [],
                    aiUnavailableFallback: Boolean(response.aiUnavailableFallback),
                    providerError: response.providerError || null,
                    localAnswersNotSentToAI: response.localAnswersNotSentToAI || preview.localAnswersNotSentToAI || [],
                    lockedManualNotSentToAI: response.lockedManualNotSentToAI || preview.lockedManualNotSentToAI || []
                }
            });
        } catch (error) {
            finishProgress(error.message, 'error', false);
            setStatus(error.message, 'error');
            openPanel();
        }
    }

    function fillFormLocalOnly() {
        return fillFormAutomatically({ config: { provider: 'local_rules' } });
    }

    function setNativeValue(element, value) {
        const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const descriptor = Object.getOwnPropertyDescriptor(prototype, 'value');
        if (descriptor && descriptor.set) descriptor.set.call(element, value);
        else element.value = value;
    }

    function dispatchFormEvents(element) {
        ['input', 'change', 'blur'].forEach((eventName) => {
            element.dispatchEvent(new Event(eventName, { bubbles: true }));
        });
    }

    function fillTextElement(element, answer) {
        setNativeValue(element, String(answer));
        dispatchFormEvents(element);
        return true;
    }

    function splitAnswerValues(answer) {
        if (Array.isArray(answer)) return answer.map(String);
        return String(answer).split(',').map((part) => part.trim()).filter(Boolean);
    }

    function matchSingleOption(options, answer) {
        const target = normalizeKey(answer);
        const exactMatches = options.filter((option) => normalizeText(option.label) === normalizeText(answer));
        if (exactMatches.length === 1) return { option: exactMatches[0], confidence: 'exact' };
        if (exactMatches.length > 1) return { error: 'ambiguous exact option match' };

        const normalizedMatches = options.filter((option) => normalizeKey(option.label) === target);
        if (normalizedMatches.length === 1) return { option: normalizedMatches[0], confidence: 'normalized' };
        if (normalizedMatches.length > 1) return { error: 'ambiguous normalized option match' };

        return { error: 'no confident option match' };
    }

    function getChoiceOptions(container, selector) {
        return Array.from(container.querySelectorAll(selector))
            .filter(isVisible)
            .map((element) => ({ element, label: optionText(element) }))
            .filter((option) => option.label);
    }

    function fillRadio(container, answer) {
        const options = getChoiceOptions(container, SELECTORS.radio);
        const match = matchSingleOption(options, answer);
        if (!match.option) return { ok: false, reason: match.error || 'unresolved radio option' };
        if (match.option.element.getAttribute('aria-checked') !== 'true') activateChoice(match.option.element);
        return { ok: true };
    }

    function activateChoice(element) {
        element.scrollIntoView({ behavior: 'smooth', block: 'center' });
        try {
            ['mousedown', 'mouseup', 'click'].forEach((eventName) => {
                element.dispatchEvent(new MouseEvent(eventName, { bubbles: true, cancelable: true }));
            });
        } catch (_) {
            element.click();
        }
    }

    function findGlobalCheckboxOption(value) {
        const options = getChoiceOptions(document, SELECTORS.checkbox);
        const match = matchSingleOption(options, value);
        return match.option || null;
    }

    function fillCheckbox(container, answer) {
        const values = splitAnswerValues(answer);
        const options = getChoiceOptions(container, SELECTORS.checkbox);
        const selected = [];
        const unresolved = [];

        values.forEach((value) => {
            const match = matchSingleOption(options, value);
            if (!match.option) {
                unresolved.push(`${value}: ${match.error || 'unresolved checkbox option'}`);
                return;
            }
            if (match.option.element.getAttribute('aria-checked') !== 'true') activateChoice(match.option.element);
            selected.push(value);
        });

        if (unresolved.length) {
            const stillUnresolved = [];
            unresolved.forEach((item) => {
                const value = item.split(':')[0];
                const globalOption = findGlobalCheckboxOption(value);
                if (!globalOption) {
                    stillUnresolved.push(item);
                    return;
                }
                if (globalOption.element.getAttribute('aria-checked') !== 'true') activateChoice(globalOption.element);
                selected.push(value);
            });
            if (stillUnresolved.length) return { ok: false, reason: stillUnresolved.join('; ') };
        }
        return { ok: selected.length > 0, reason: selected.length ? '' : 'empty checkbox answer' };
    }

    async function fillDropdown(container, answer) {
        const trigger = container.querySelector(SELECTORS.dropdownTrigger);
        if (!trigger) return { ok: false, reason: 'dropdown trigger not found' };

        trigger.click();
        await delay(100);

        const allOptions = unique([
            ...extractOptions(container, SELECTORS.dropdownOption),
            ...Array.from(document.querySelectorAll(SELECTORS.dropdownOption)).map(optionText)
        ]);
        const matches = allOptions.filter((label) => normalizeKey(label) === normalizeKey(answer));
        if (matches.length !== 1) {
            document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
            return { ok: false, reason: matches.length > 1 ? 'ambiguous dropdown option match' : 'no confident dropdown option match' };
        }

        const optionElement = Array.from(document.querySelectorAll(SELECTORS.dropdownOption))
            .find((option) => normalizeKey(optionText(option)) === normalizeKey(answer));
        if (!optionElement) {
            document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
            return { ok: false, reason: 'matching dropdown option was not visible after opening' };
        }

        optionElement.click();
        return { ok: true };
    }

    function normalizeDateValue(answer) {
        const value = String(answer).trim();
        if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
        const match = value.match(/^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$/);
        if (match) return `${match[3]}-${match[2].padStart(2, '0')}-${match[1].padStart(2, '0')}`;
        return value;
    }

    function buildAnswerResolver(answers, questions) {
        const exactTitle = new Map();
        const normalizedTitle = new Map();
        const id = new Map();

        questions.forEach((question) => {
            id.set(question.id, question);
            exactTitle.set(question.title, question);
            const key = normalizeKey(question.title);
            if (!normalizedTitle.has(key)) normalizedTitle.set(key, []);
            normalizedTitle.get(key).push(question);
        });

        return function resolve(question) {
            if (Object.prototype.hasOwnProperty.call(answers, question.id)) {
                return { found: true, answer: answers[question.id], key: question.id };
            }
            if (Object.prototype.hasOwnProperty.call(answers, question.title)) {
                return { found: true, answer: answers[question.title], key: question.title };
            }

            const normalizedQuestion = normalizeKey(question.title);
            const matchingKeys = Object.keys(answers).filter((key) => normalizeKey(key) === normalizedQuestion);
            if (matchingKeys.length === 1) {
                return { found: true, answer: answers[matchingKeys[0]], key: matchingKeys[0] };
            }
            if (matchingKeys.length > 1) {
                return { found: false, ambiguous: true, reason: 'multiple answer keys normalize to this title' };
            }

            return { found: false };
        };
    }

    async function fillFormFromAnswers(options = {}) {
        const extraction = lastExtraction || extractQuestions();
        let answers;

        try {
            answers = JSON.parse(getTextareaValue('answers') || '{}');
        } catch (error) {
            setStatus(`Answers JSON is invalid: ${error.message}`, 'error');
            return;
        }

        if (!answers || typeof answers !== 'object' || Array.isArray(answers)) {
            setStatus('Answers JSON must be an object.', 'error');
            return;
        }

        const containers = getQuestionContainers();
        const questions = extraction.questions.map((question, index) => ({
            ...question,
            container: containers[index]
        })).filter((question) => question.container);
        const resolveAnswer = buildAnswerResolver(answers, questions);
        const report = {
            routing: options.routing || null,
            filled: [],
            skipped: [],
            manualRequired: [],
            unresolved: [],
            errors: []
        };

        for (const question of questions) {
            if (options.progress) {
                const processed = report.filled.length + report.skipped.length + report.manualRequired.length + report.unresolved.length + report.errors.length;
                const percent = 58 + Math.round((processed / Math.max(questions.length, 1)) * 36);
                updateProgress('Filling Google Form...', `Processing: ${question.title}`, percent);
            }
            clearQuestionMark(question.container);
            const resolved = resolveAnswer(question);
            if (!resolved.found) {
                report.skipped.push({ id: question.id, title: question.title, reason: resolved.reason || 'no answer provided' });
                continue;
            }

            const answer = resolved.answer;
            if (answer === MANUAL_REQUIRED) {
                markQuestion(question.container, 'manual');
                report.manualRequired.push({ id: question.id, title: question.title });
                continue;
            }
            if (answer === null || answer === undefined || String(answer).trim() === '') {
                report.skipped.push({ id: question.id, title: question.title, reason: 'empty answer' });
                continue;
            }

            try {
                let result = { ok: false, reason: 'unsupported field type' };
                if (question.type === 'short_text' || question.type === 'email' || question.type === 'phone') {
                    const input = question.container.querySelector(SELECTORS.textInput);
                    result = input ? { ok: fillTextElement(input, answer) } : { ok: false, reason: 'text input not found' };
                } else if (question.type === 'paragraph') {
                    const textarea = question.container.querySelector(SELECTORS.textarea);
                    result = textarea ? { ok: fillTextElement(textarea, answer) } : { ok: false, reason: 'textarea not found' };
                } else if (question.type === 'date') {
                    const input = question.container.querySelector(SELECTORS.dateInput) || question.container.querySelector(SELECTORS.textInput);
                    result = input ? { ok: fillTextElement(input, normalizeDateValue(answer)) } : { ok: false, reason: 'date input not found' };
                } else if (question.type === 'radio') {
                    result = fillRadio(question.container, answer);
                } else if (question.type === 'checkbox') {
                    result = fillCheckbox(question.container, answer);
                } else if (question.type === 'dropdown') {
                    result = await fillDropdown(question.container, answer);
                } else if (question.type === 'file_upload') {
                    result = { ok: false, reason: 'file upload must be completed manually' };
                }

                if (result.ok) {
                    report.filled.push({ id: question.id, title: question.title, key: resolved.key });
                } else {
                    markQuestion(question.container, 'unresolved');
                    report.unresolved.push({ id: question.id, title: question.title, answer, reason: result.reason });
                }
            } catch (error) {
                markQuestion(question.container, 'unresolved');
                report.errors.push({ id: question.id, title: question.title, message: error.message });
            }
        }

        renderReport(report);
        setStatus(`Fill complete: ${report.filled.length} filled, ${report.manualRequired.length} manual, ${report.unresolved.length} unresolved. Review before submitting manually.`, report.unresolved.length || report.errors.length ? 'warn' : 'ok');
        if (options.progress) {
            const level = report.unresolved.length || report.errors.length ? 'warn' : 'ok';
            finishProgress(`Complete: ${report.filled.length} filled, ${report.manualRequired.length} manual, ${report.unresolved.length} unresolved.`, level);
        }
    }

    function delay(ms) {
        return new Promise((resolve) => setTimeout(resolve, ms));
    }

    function copyText(text) {
        try {
            if (typeof GM_setClipboard === 'function') {
                GM_setClipboard(text, 'text');
                return true;
            }
        } catch (_) {
            // Fall through to navigator clipboard.
        }

        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(text).catch(() => false);
            return true;
        }
        return false;
    }

    function getTextareaValue(name) {
        const el = document.getElementById(`${APP_ID}-${name}`);
        return el ? el.value : '';
    }

    function setTextareaValue(name, value) {
        const el = document.getElementById(`${APP_ID}-${name}`);
        if (el) el.value = value;
    }

    function setStatus(message, level) {
        const el = document.getElementById(`${APP_ID}-status`);
        if (!el) return;
        el.textContent = message;
        el.dataset.level = level || 'info';
    }

    function providerIssueMessage(response) {
        if (!response || !response.aiUnavailableFallback) return '';
        const provider = response.providerError && response.providerError.provider ? response.providerError.provider : 'AI provider';
        const rawStatus = response.providerError && response.providerError.status;
        const failedCount = Array.isArray(response.apiQuestionsFailed) ? response.apiQuestionsFailed.length : Number(response.apiQuestionCount) || 0;
        if (rawStatus === 'invalid_response') {
            return `${provider} returned an invalid response. Filled local answers only; ${failedCount} API question(s) need manual review this time.`;
        }
        const status = rawStatus ? ` HTTP ${rawStatus}` : '';
        return `${provider}${status} is unavailable or overloaded. Filled local answers only; ${failedCount} API question(s) need manual review this time.`;
    }

    function clearQuestionMark(container) {
        if (!container) return;
        container.removeAttribute('data-gfc-status');
    }

    function markQuestion(container, status) {
        if (!container) return;
        container.setAttribute('data-gfc-status', status);
    }

    function renderReport(report) {
        const el = document.getElementById(`${APP_ID}-report`);
        if (!el) return;
        el.textContent = JSON.stringify(report, null, 2);
    }

    function copyReport() {
        const report = document.getElementById(`${APP_ID}-report`);
        if (!report) return;
        const copied = copyText(report.textContent || '{}');
        setStatus(copied ? 'Last fill report copied to clipboard.' : 'Clipboard permission is unavailable. Select and copy the report manually.', copied ? 'ok' : 'warn');
    }

    function showProgress(title, detail, percent) {
        if (!activeProgress) {
            activeProgress = createProgress();
        }
        activeProgress.container.style.display = 'block';
        updateProgress(title, detail, percent);
    }

    function createProgress() {
        const container = document.createElement('div');
        container.id = `${APP_ID}-progress`;

        const close = document.createElement('button');
        close.type = 'button';
        close.className = 'gfc-progress-close';
        close.textContent = '×';
        close.title = 'Close';
        close.addEventListener('click', () => {
            container.style.display = 'none';
        });

        const title = document.createElement('div');
        title.className = 'gfc-progress-title';

        const barOuter = document.createElement('div');
        barOuter.className = 'gfc-progress-outer';

        const bar = document.createElement('div');
        bar.className = 'gfc-progress-bar';
        barOuter.appendChild(bar);

        const detail = document.createElement('div');
        detail.className = 'gfc-progress-detail';

        container.append(close, title, barOuter, detail);
        document.body.appendChild(container);
        addStyles();
        return { container, title, bar, detail };
    }

    function updateProgress(title, detail, percent) {
        if (!activeProgress) return;
        const safePercent = Math.max(0, Math.min(100, Number(percent) || 0));
        activeProgress.container.dataset.level = 'info';
        activeProgress.title.textContent = title;
        activeProgress.detail.textContent = detail;
        activeProgress.bar.style.width = `${safePercent}%`;
    }

    function finishProgress(detail, level = 'ok', autoHide = true) {
        if (!activeProgress) return;
        activeProgress.container.dataset.level = level;
        activeProgress.title.textContent = level === 'error' ? 'Form filling stopped' : 'Form filling complete';
        activeProgress.detail.textContent = detail;
        activeProgress.bar.style.width = '100%';
        if (autoHide) {
            setTimeout(() => {
                if (activeProgress) activeProgress.container.style.display = 'none';
            }, 3500);
        }
    }

    function clearPanel() {
        setTextareaValue('questions', '');
        setTextareaValue('answers', '');
        renderReport({});
        setStatus('Cleared local textareas. No form values were changed.', 'info');
        document.querySelectorAll('[data-gfc-status]').forEach(clearQuestionMark);
    }

    function createButton(label, onClick) {
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = label;
        button.addEventListener('click', onClick);
        return button;
    }

    const OPTION_HELP = {
        provider: {
            auto: 'Auto chooses the best available engine: Gemini/OpenAI if configured, otherwise local rules.',
            local_rules: 'Local rules only uses your local common answers. No AI provider request is made.',
            google: 'Google Gemini sends filtered question/context data to Google through the localhost server.',
            openai: 'OpenAI sends filtered question/context data to OpenAI through the localhost server.'
        }
    };

    function createSelect(id, options) {
        const select = document.createElement('select');
        select.id = id;
        options.forEach((option) => {
            const el = document.createElement('option');
            el.value = option.value;
            el.textContent = option.label;
            if (option.help) el.title = option.help;
            select.appendChild(el);
        });
        return select;
    }

    function createHelp(id) {
        const help = document.createElement('div');
        help.id = id;
        help.className = 'gfc-help';
        return help;
    }

    function setSelectHelp(select, help, helpMap) {
        const value = select.value;
        const text = helpMap[value] || '';
        select.title = text;
        help.textContent = text;
    }

    function syncConfigFields(config) {
        const profile = document.getElementById(`${APP_ID}-profile`);
        const provider = document.getElementById(`${APP_ID}-provider`);
        if (profile) profile.value = config.activeProfile || DEFAULT_CONFIG.activeProfile;
        if (provider) provider.value = config.provider || DEFAULT_CONFIG.provider;
        updateConfigHelp();
    }

    function readConfigFields() {
        return {
            activeProfile: document.getElementById(`${APP_ID}-profile`)?.value || DEFAULT_CONFIG.activeProfile,
            provider: document.getElementById(`${APP_ID}-provider`)?.value || DEFAULT_CONFIG.provider
        };
    }

    function renderHealth(body) {
        const health = document.getElementById(`${APP_ID}-health`);
        if (!health) return;
        if (!body || body.error) {
            health.textContent = body?.error || 'Localhost server unavailable.';
            return;
        }
        const config = body.config || getLocalConfig();
        health.textContent = [
            `Mode: ${body.mode || 'unknown'}`,
            `Profile: ${config.activeProfile || 'unknown'}`,
            `Provider setting: ${config.provider || 'auto'}`,
            `Google key: ${body.googleKey ? `yes (${body.googleKeySource || 'unknown'})` : 'no'}`,
            `OpenAI key: ${body.openaiKey ? 'yes' : 'no'}`
        ].join('\n');
    }

    async function refreshConfigPanel() {
        const localConfig = getLocalConfig();
        syncConfigFields(localConfig);
        setStatus('Checking localhost config...', 'info');
        try {
            const body = await localRequestJson(`${LOCAL_BASE_URL}/config`, null, 'GET');
            const serverConfig = body.config || localConfig;
            setLocalConfig(serverConfig);

            const profileSelect = document.getElementById(`${APP_ID}-profile`);
            if (profileSelect && Array.isArray(body.profiles)) {
                while (profileSelect.firstChild) profileSelect.removeChild(profileSelect.firstChild);
                body.profiles.forEach((profile) => {
                    const option = document.createElement('option');
                    option.value = profile.id;
                    option.textContent = `${profile.label || profile.id}${profile.hasCommonAnswers ? '' : ' (no common answers)'}`;
                    profileSelect.appendChild(option);
                });
            }

            syncConfigFields(serverConfig);
            renderHealth(body);
            setStatus('Config loaded from localhost.', 'ok');
        } catch (error) {
            renderHealth({ error: error.message });
            setStatus(`${error.message}\nStart localhost in PowerShell:\n${START_SERVER_COMMAND}`, 'warn');
        }
    }

    async function persistConfigPanel() {
        const config = setLocalConfig(readConfigFields());
        try {
            const body = await localRequestJson(`${LOCAL_BASE_URL}/config`, { config });
            if (body.config) setLocalConfig(body.config);
            syncConfigFields(body.config || config);
            renderHealth(body);
            setStatus('Config saved automatically. Fill form will use these settings.', 'ok');
        } catch (error) {
            renderHealth({ error: error.message });
            setStatus(`${error.message}\nSaved in browser only. Start localhost in PowerShell:\n${START_SERVER_COMMAND}`, 'warn');
        }
    }

    function updateConfigHelp() {
        const provider = document.getElementById(`${APP_ID}-provider`);
        const providerHelp = document.getElementById(`${APP_ID}-provider-help`);
        if (provider && providerHelp) setSelectHelp(provider, providerHelp, OPTION_HELP.provider);
    }

    function addPanel() {
        if (document.getElementById(APP_ID)) return;

        const panel = document.createElement('section');
        panel.id = APP_ID;
        panel.setAttribute('aria-label', 'Formizzy');

        const title = document.createElement('h2');
        title.textContent = `Formizzy Config v${SCRIPT_VERSION}`;

        const form = document.createElement('div');
        form.className = 'gfc-config-grid';

        const profileLabel = document.createElement('label');
        profileLabel.textContent = 'Profile';
        profileLabel.htmlFor = `${APP_ID}-profile`;
        const profile = createSelect(`${APP_ID}-profile`, [{ value: 'erasmus', label: 'Erasmus' }]);
        profile.addEventListener('change', persistConfigPanel);

        const providerLabel = document.createElement('label');
        providerLabel.textContent = 'Answer engine';
        providerLabel.htmlFor = `${APP_ID}-provider`;
        const provider = createSelect(`${APP_ID}-provider`, [
            { value: 'auto', label: 'Auto', help: OPTION_HELP.provider.auto },
            { value: 'local_rules', label: 'Local rules only', help: OPTION_HELP.provider.local_rules },
            { value: 'google', label: 'Google Gemini', help: OPTION_HELP.provider.google },
            { value: 'openai', label: 'OpenAI', help: OPTION_HELP.provider.openai }
        ]);
        provider.addEventListener('change', () => {
            updateConfigHelp();
            persistConfigPanel();
        });
        const providerHelp = createHelp(`${APP_ID}-provider-help`);

        form.append(profileLabel, profile, providerLabel, provider, document.createElement('span'), providerHelp);

        const status = document.createElement('div');
        status.id = `${APP_ID}-status`;
        status.dataset.level = 'info';
        status.textContent = 'Choose the local profile and answer engine. Changes are saved automatically.';

        const healthLabel = document.createElement('label');
        healthLabel.textContent = 'Localhost status';
        healthLabel.htmlFor = `${APP_ID}-health`;

        const health = document.createElement('pre');
        health.id = `${APP_ID}-health`;
        health.textContent = 'Not checked yet.';

        const reportHeader = document.createElement('div');
        reportHeader.className = 'gfc-report-header';

        const reportLabel = document.createElement('label');
        reportLabel.textContent = 'Last fill report';
        reportLabel.htmlFor = `${APP_ID}-report`;

        const report = document.createElement('pre');
        report.id = `${APP_ID}-report`;
        report.textContent = '{}';

        const copyReportButton = createButton('Copy', copyReport);
        copyReportButton.className = 'gfc-copy-report';
        copyReportButton.title = 'Copy the report JSON to clipboard';
        reportHeader.append(reportLabel, copyReportButton);

        const questions = document.createElement('textarea');
        questions.id = `${APP_ID}-questions`;
        questions.hidden = true;
        questions.spellcheck = false;

        const answers = document.createElement('textarea');
        answers.id = `${APP_ID}-answers`;
        answers.hidden = true;
        answers.spellcheck = false;

        panel.append(title, form, status, healthLabel, health, reportHeader, report, questions, answers);
        document.body.appendChild(panel);
        addStyles();
        syncConfigFields(getLocalConfig());
        updateConfigHelp();
    }

    function openPanel() {
        const panel = document.getElementById(APP_ID);
        const overlay = document.getElementById(`${APP_ID}-overlay`);
        if (panel) panel.style.display = 'block';
        if (overlay) overlay.style.display = 'block';
        refreshConfigPanel();
    }

    function closePanel() {
        const panel = document.getElementById(APP_ID);
        const overlay = document.getElementById(`${APP_ID}-overlay`);
        if (panel) panel.style.display = 'none';
        if (overlay) overlay.style.display = 'none';
    }

    function createMenuItem(label, onClick) {
        const item = document.createElement('button');
        item.type = 'button';
        item.textContent = label;
        item.addEventListener('click', () => {
            const menu = document.getElementById(`${APP_ID}-menu`);
            if (menu) menu.style.display = 'none';
            onClick();
        });
        return item;
    }

    function addLauncher() {
        if (document.getElementById(`${APP_ID}-launcher`)) return;

        const launcher = document.createElement('button');
        launcher.id = `${APP_ID}-launcher`;
        launcher.type = 'button';
        launcher.title = 'Formizzy - make it easy';

        const icon = document.createElement('img');
        icon.src = 'https://cdn-icons-png.flaticon.com/512/17113/17113805.png';
        icon.alt = 'Form Filler';
        launcher.appendChild(icon);

        const menu = document.createElement('div');
        menu.id = `${APP_ID}-menu`;
        menu.append(
            createMenuItem('Fill form', fillFormAutomatically),
            createMenuItem('Open panel', openPanel),
            createMenuItem('Clear Marks', clearPanel)
        );

        launcher.addEventListener('click', (event) => {
            event.stopPropagation();
            menu.style.display = menu.style.display === 'block' ? 'none' : 'block';
        });

        document.addEventListener('click', (event) => {
            if (!menu.contains(event.target) && event.target !== launcher) {
                menu.style.display = 'none';
            }
        });

        document.body.append(launcher, menu);
    }

    function addStyles() {
        if (document.getElementById(`${APP_ID}-styles`)) return;
        const style = document.createElement('style');
        style.id = `${APP_ID}-styles`;
        style.textContent = `
            #${APP_ID}-overlay {
                display: none;
                position: fixed;
                inset: 0;
                z-index: 2147483645;
                background: rgba(0, 0, 0, 0.35);
            }
            #${APP_ID} {
                display: none;
                position: fixed;
                top: 50%;
                left: 50%;
                transform: translate(-50%, -50%);
                z-index: 2147483647;
                width: min(720px, calc(100vw - 32px));
                max-height: calc(100vh - 64px);
                overflow: auto;
                box-sizing: border-box;
                padding: 12px;
                border: 1px solid #d0d7de;
                border-radius: 8px;
                background: #ffffff;
                color: #24292f;
                box-shadow: 0 8px 24px rgba(140, 149, 159, 0.25);
                font: 13px/1.4 Arial, sans-serif;
            }
            #${APP_ID} h2 {
                margin: 0 0 10px;
                font-size: 15px;
                line-height: 1.2;
            }
            #${APP_ID} label {
                display: block;
                margin: 10px 0 4px;
                font-weight: 700;
                font-size: 12px;
            }
            #${APP_ID} textarea {
                width: 100%;
                height: 132px;
                box-sizing: border-box;
                resize: vertical;
                padding: 8px;
                border: 1px solid #d0d7de;
                border-radius: 6px;
                font: 12px/1.35 Consolas, "Courier New", monospace;
                color: #24292f;
                background: #f6f8fa;
            }
            #${APP_ID} select {
                width: 100%;
                min-height: 34px;
                box-sizing: border-box;
                padding: 6px 8px;
                border: 1px solid #d0d7de;
                border-radius: 6px;
                background: #ffffff;
                color: #24292f;
                font: 13px/1.3 Arial, sans-serif;
            }
            #${APP_ID} .gfc-config-grid {
                display: grid;
                grid-template-columns: 150px 1fr;
                gap: 8px;
                align-items: center;
            }
            #${APP_ID} .gfc-config-grid label {
                margin: 0;
            }
            #${APP_ID} .gfc-help {
                margin: -3px 0 3px;
                color: #57606a;
                font-size: 12px;
                line-height: 1.35;
            }
            #${APP_ID} .gfc-report-header {
                display: flex;
                align-items: center;
                justify-content: flex-start;
                gap: 8px;
                margin-top: 10px;
            }
            #${APP_ID} .gfc-report-header label {
                margin: 0;
            }
            #${APP_ID} .gfc-copy-report {
                min-height: 25px;
                padding: 3px 7px;
                border-radius: 5px;
                font-size: 11px;
                white-space: nowrap;
            }
            #${APP_ID} .gfc-buttons {
                display: grid;
                grid-template-columns: repeat(2, 1fr);
                gap: 6px;
                margin-top: 12px;
            }
            #${APP_ID} button {
                min-height: 32px;
                padding: 6px 8px;
                border: 1px solid #8c959f;
                border-radius: 6px;
                background: #f6f8fa;
                color: #24292f;
                cursor: pointer;
                font: 12px/1.2 Arial, sans-serif;
            }
            #${APP_ID} button:hover {
                background: #eaeef2;
            }
            #${APP_ID}-launcher {
                position: fixed;
                right: 20px;
                bottom: 20px;
                z-index: 2147483647;
                width: 54px;
                height: 54px;
                border: none;
                border-radius: 50%;
                background: #473080;
                padding: 8px;
                font: 700 15px/1 Arial, sans-serif;
                box-shadow: 0 6px 18px rgba(0, 0, 0, 0.35);
                cursor: pointer;
            }
            #${APP_ID}-launcher:hover {
                transform: scale(1.06);
            }
            #${APP_ID}-launcher img {
                display: block;
                width: 100%;
                height: 100%;
                object-fit: contain;
            }
            #${APP_ID}-menu {
                display: none;
                position: fixed;
                right: 20px;
                bottom: 84px;
                z-index: 2147483647;
                width: 240px;
                padding: 8px;
                border-radius: 12px;
                background: #ffffff;
                box-shadow: 0 8px 24px rgba(0, 0, 0, 0.3);
                font: 13px/1.3 Arial, sans-serif;
            }
            #${APP_ID}-menu button {
                display: block;
                width: 100%;
                min-height: 38px;
                margin: 0;
                padding: 9px 10px;
                border: 0;
                border-top: 1px solid #eeeeee;
                border-radius: 8px;
                background: #ffffff;
                color: #24292f;
                text-align: left;
                cursor: pointer;
            }
            #${APP_ID}-menu button:first-child {
                border-top: 0;
            }
            #${APP_ID}-menu button:hover {
                background: #f6f8fa;
            }
            #${APP_ID}-status {
                margin-top: 10px;
                padding: 8px;
                border-radius: 6px;
                background: #ddf4ff;
                color: #0969da;
                white-space: pre-wrap;
            }
            #${APP_ID}-status[data-level="ok"] {
                background: #dafbe1;
                color: #1a7f37;
            }
            #${APP_ID}-status[data-level="warn"] {
                background: #fff8c5;
                color: #9a6700;
            }
            #${APP_ID}-status[data-level="error"] {
                background: #ffebe9;
                color: #cf222e;
            }
            #${APP_ID}-progress {
                display: none;
                position: fixed;
                top: 50%;
                left: 50%;
                transform: translate(-50%, -50%);
                z-index: 2147483647;
                width: min(420px, calc(100vw - 40px));
                box-sizing: border-box;
                padding: 22px;
                border-radius: 14px;
                background: rgba(255, 255, 255, 0.98);
                color: #24292f;
                box-shadow: 0 12px 38px rgba(0, 0, 0, 0.32);
                font: 13px/1.4 Arial, sans-serif;
                text-align: center;
            }
            #${APP_ID}-progress .gfc-progress-title {
                margin-bottom: 14px;
                color: #24292f;
                font-size: 17px;
                font-weight: 700;
            }
            #${APP_ID}-progress .gfc-progress-close {
                position: absolute;
                top: 8px;
                right: 8px;
                width: 26px;
                height: 26px;
                min-height: 0;
                padding: 0;
                border: 0;
                border-radius: 50%;
                background: transparent;
                color: #57606a;
                font: 20px/24px Arial, sans-serif;
                cursor: pointer;
            }
            #${APP_ID}-progress .gfc-progress-close:hover {
                background: #f6f8fa;
                color: #24292f;
            }
            #${APP_ID}-progress .gfc-progress-outer {
                width: 100%;
                height: 8px;
                overflow: hidden;
                border-radius: 999px;
                background: #d8dee4;
            }
            #${APP_ID}-progress .gfc-progress-bar {
                width: 0%;
                height: 100%;
                border-radius: 999px;
                background: linear-gradient(90deg, #2da44e, #6fdd8b);
                transition: width 0.25s ease;
            }
            #${APP_ID}-progress .gfc-progress-detail {
                margin-top: 12px;
                color: #57606a;
                font-size: 13px;
                text-align: left;
                white-space: pre-wrap;
            }
            #${APP_ID}-progress[data-level="ok"] .gfc-progress-title {
                color: #1a7f37;
            }
            #${APP_ID}-progress[data-level="warn"] .gfc-progress-title {
                color: #9a6700;
            }
            #${APP_ID}-progress[data-level="error"] .gfc-progress-title {
                color: #cf222e;
            }
            #${APP_ID}-progress[data-level="error"] .gfc-progress-bar {
                background: linear-gradient(90deg, #cf222e, #ff8182);
            }
            #${APP_ID}-report,
            #${APP_ID}-health {
                max-height: 140px;
                overflow: auto;
                margin: 0;
                padding: 8px;
                border-radius: 6px;
                background: #f6f8fa;
                font: 12px/1.35 Consolas, "Courier New", monospace;
                white-space: pre-wrap;
            }
            [data-gfc-status="manual"] {
                outline: 3px solid #bf8700 !important;
                outline-offset: 3px !important;
            }
            [data-gfc-status="unresolved"] {
                outline: 3px solid #cf222e !important;
                outline-offset: 3px !important;
            }
            @media (max-width: 720px) {
                #${APP_ID} {
                    top: 8px;
                    right: 8px;
                    left: 8px;
                    bottom: auto;
                    transform: none;
                    width: auto;
                    max-height: calc(100vh - 16px);
                }
                #${APP_ID} .gfc-buttons {
                    grid-template-columns: 1fr;
                }
                #${APP_ID} .gfc-config-grid {
                    grid-template-columns: 1fr;
                }
            }
        `;
        document.head.appendChild(style);
    }

    function init() {
        addPanel();
        const overlay = document.createElement('div');
        overlay.id = `${APP_ID}-overlay`;
        overlay.addEventListener('click', closePanel);
        document.body.appendChild(overlay);
        addLauncher();
        console.info('Formizzy loaded. It never submits forms. Localhost drafting requires local_forms_ai_server.py.');
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
