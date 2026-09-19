// Prompt Builder - Single Page Application
(function () {
    "use strict";

    // State
    let currentSessionId = null;
    let currentSessionStatus = null;
    let isLoading = false;
    let allSessions = [];
    let activeFilter = "all";
    let searchQuery = "";
    let isImproveMode = false;

    // DOM Elements
    const emptyState = document.getElementById("empty-state");
    const messageList = document.getElementById("message-list");
    const chatContainer = document.getElementById("chat-container");
    const historyList = document.getElementById("history-list");
    const searchInput = document.getElementById("search-input");
    const inputText = document.getElementById("input-text");
    const btnSend = document.getElementById("btn-send");
    const btnNewPrompt = document.getElementById("btn-new-prompt");
    const btnToggleMode = document.getElementById("btn-toggle-mode");
    const skipContainer = document.getElementById("skip-container");
    const btnSkip = document.getElementById("btn-skip");
    const loadingIndicator = document.getElementById("loading-indicator");
    const btnToggleSidebar = document.getElementById("btn-toggle-sidebar");
    const sidebar = document.getElementById("sidebar");
    const sidebarOverlay = document.getElementById("sidebar-overlay");

    // Initialize
    function init() {
        bindEvents();
        loadHistory();
        resetToEmptyState();
    }

    // Bind UI Event Listeners
    function bindEvents() {
        btnNewPrompt.addEventListener("click", function () {
            isImproveMode = false;
            updateModeUI();
            resetToEmptyState();
        });

        if (btnToggleMode) {
            btnToggleMode.addEventListener("click", function () {
                isImproveMode = !isImproveMode;
                updateModeUI();
                resetToEmptyState();
            });
        }

        btnSend.addEventListener("click", handleSend);

        // Keyboard shortcuts
        document.addEventListener("keydown", function (e) {
            // Ctrl+K / Cmd+K to focus input
            if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
                e.preventDefault();
                inputText.focus();
                inputText.select();
            }
            // Ctrl+Enter / Cmd+Enter to send
            if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
                e.preventDefault();
                handleSend();
            }
        });

        inputText.addEventListener("keydown", function (e) {
            if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                handleSend();
            }
        });

        inputText.addEventListener("input", autoResizeTextarea);

        btnSkip.addEventListener("click", handleSkip);

        // Example pills
        document.querySelectorAll(".pill-btn").forEach(function (pill) {
            pill.addEventListener("click", function () {
                const idea = pill.getAttribute("data-idea");
                if (idea) {
                    inputText.value = idea;
                    autoResizeTextarea();
                    handleSend();
                }
            });
        });

        // Search input
        if (searchInput) {
            searchInput.addEventListener("input", function () {
                searchQuery = searchInput.value.trim().toLowerCase();
                renderFilteredHistory();
            });
        }

        // Filter chips
        document.querySelectorAll(".chip").forEach(function (chip) {
            chip.addEventListener("click", function () {
                document.querySelectorAll(".chip").forEach(function (c) {
                    c.classList.remove("active");
                });
                chip.classList.add("active");
                activeFilter = chip.getAttribute("data-filter") || "all";
                renderFilteredHistory();
            });
        });

        // Mobile drawer toggles
        if (btnToggleSidebar) {
            btnToggleSidebar.addEventListener("click", function () {
                sidebar.classList.toggle("open");
                sidebarOverlay.classList.toggle("active");
            });
        }

        if (sidebarOverlay) {
            sidebarOverlay.addEventListener("click", function () {
                sidebar.classList.remove("open");
                sidebarOverlay.classList.remove("active");
            });
        }
    }

    // Update Mode UI state
    function updateModeUI() {
        if (!btnToggleMode) return;
        btnToggleMode.classList.toggle("active", isImproveMode);
        btnToggleMode.setAttribute("aria-pressed", isImproveMode ? "true" : "false");

        const emptyTitle = emptyState.querySelector("h2");
        const emptyDesc = emptyState.querySelector("p");

        if (isImproveMode) {
            if (emptyTitle) emptyTitle.textContent = "Improve an existing prompt";
            if (emptyDesc) emptyDesc.textContent = "Paste any draft or existing prompt below. The assistant will transform it into a structured, model-agnostic prompt and detail the changes made.";
            inputText.placeholder = "Paste your existing prompt here to improve it... (Ctrl+K to focus, Enter to send)";
        } else {
            if (emptyTitle) emptyTitle.textContent = "What do you want a prompt for?";
            if (emptyDesc) emptyDesc.textContent = "Enter a rough idea below. The assistant will ask a few quick questions to craft an optimal, model-agnostic prompt.";
            inputText.placeholder = "Type your idea or answer... (Ctrl+K to focus, Enter to send)";
        }
    }

    // Auto-resize textarea height
    function autoResizeTextarea() {
        inputText.style.height = "auto";
        inputText.style.height = Math.min(inputText.scrollHeight, 160) + "px";
    }

    // Set Loading State
    function setLoading(loading) {
        isLoading = loading;
        loadingIndicator.style.display = loading ? "flex" : "none";
        btnSend.disabled = loading;
        btnSkip.disabled = loading;
        inputText.disabled = loading;
        if (loading) {
            scrollToBottom();
        } else {
            inputText.focus();
        }
    }

    // Scroll chat to the bottom
    function scrollToBottom() {
        requestAnimationFrame(function () {
            chatContainer.scrollTop = chatContainer.scrollHeight;
        });
    }

    // Reset UI to Empty State
    function resetToEmptyState() {
        currentSessionId = null;
        currentSessionStatus = null;
        messageList.innerHTML = "";
        messageList.style.display = "none";
        emptyState.style.display = "flex";
        skipContainer.style.display = "none";
        inputText.value = "";
        autoResizeTextarea();
        inputText.focus();

        // Remove active class from history
        document.querySelectorAll(".history-item").forEach(function (item) {
            item.classList.remove("active");
        });

        // Close mobile drawer if open
        sidebar.classList.remove("open");
        sidebarOverlay.classList.remove("active");
    }

    // Fetch and render sidebar history
    async function loadHistory() {
        try {
            const resp = await fetch("/api/sessions");
            if (!resp.ok) return;
            allSessions = await resp.json();
            renderFilteredHistory();
        } catch (err) {
            console.error("Failed to load history", err);
        }
    }

    // Filter and render history list
    function renderFilteredHistory() {
        const filtered = allSessions.filter(function (sess) {
            const matchesType = activeFilter === "all" || sess.type === activeFilter;
            const matchesSearch = !searchQuery || (sess.idea && sess.idea.toLowerCase().includes(searchQuery));
            return matchesType && matchesSearch;
        });
        renderHistory(filtered);
    }

    // Render history items in sidebar
    function renderHistory(sessions) {
        historyList.innerHTML = "";
        if (!sessions || sessions.length === 0) {
            const emptyMsg = document.createElement("div");
            emptyMsg.className = "history-empty";
            emptyMsg.textContent = searchQuery || activeFilter !== "all" ? "No matching sessions" : "No prompt history yet";
            emptyMsg.style.padding = "12px 16px";
            emptyMsg.style.fontSize = "0.85rem";
            emptyMsg.style.color = "var(--text-muted)";
            historyList.appendChild(emptyMsg);
            return;
        }

        sessions.forEach(function (sess) {
            const item = document.createElement("div");
            item.className = "history-item" + (sess.id === currentSessionId ? " active" : "");
            item.setAttribute("data-id", sess.id);

            const content = document.createElement("div");
            content.className = "history-content";

            const ideaSpan = document.createElement("div");
            ideaSpan.className = "history-idea";
            ideaSpan.textContent = sess.idea;

            const meta = document.createElement("div");
            meta.className = "history-meta";

            if (sess.type) {
                const tag = document.createElement("span");
                tag.className = "type-tag " + sess.type;
                tag.textContent = sess.type;
                meta.appendChild(tag);
            }

            const dateSpan = document.createElement("span");
            dateSpan.className = "history-date";
            dateSpan.textContent = formatDate(sess.created_at);
            meta.appendChild(dateSpan);

            content.appendChild(ideaSpan);
            content.appendChild(meta);

            // Delete button
            const btnDelete = document.createElement("button");
            btnDelete.className = "history-delete";
            btnDelete.setAttribute("aria-label", "Delete session");
            btnDelete.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>';
            btnDelete.addEventListener("click", function (e) {
                e.stopPropagation();
                handleDeleteSession(sess.id);
            });

            item.appendChild(content);
            item.appendChild(btnDelete);

            item.addEventListener("click", function () {
                loadSession(sess.id);
            });

            historyList.appendChild(item);
        });
    }

    // Format date string for history
    function formatDate(dateStr) {
        if (!dateStr) return "";
        try {
            const date = new Date(dateStr.replace(" ", "T") + "Z");
            if (isNaN(date.getTime())) return dateStr;
            return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
        } catch (e) {
            return dateStr;
        }
    }

    // Load a specific session into chat
    async function loadSession(sessionId) {
        if (isLoading) return;
        setLoading(true);

        try {
            const resp = await fetch("/api/sessions/" + sessionId);
            if (!resp.ok) {
                throw new Error("Failed to load session");
            }
            const data = await resp.json();
            currentSessionId = data.id;
            currentSessionStatus = data.status;

            emptyState.style.display = "none";
            messageList.style.display = "flex";
            messageList.innerHTML = "";

            // Render messages
            if (data.messages && data.messages.length > 0) {
                data.messages.forEach(function (msg) {
                    if (msg.role === "user") {
                        appendUserBubble(msg.content);
                    } else if (msg.role === "assistant") {
                        renderAssistantMessage(msg.content, data.prompts);
                    }
                });
            }

            // Update skip button visibility
            skipContainer.style.display = currentSessionStatus === "asking" ? "flex" : "none";

            // Mark active item in sidebar
            document.querySelectorAll(".history-item").forEach(function (el) {
                el.classList.toggle("active", parseInt(el.getAttribute("data-id"), 10) === currentSessionId);
            });

            // Close mobile sidebar
            sidebar.classList.remove("open");
            sidebarOverlay.classList.remove("active");

            scrollToBottom();
        } catch (err) {
            showError("Failed to load session. Please try again.", function () {
                loadSession(sessionId);
            });
        } finally {
            setLoading(false);
        }
    }

    // Delete session
    async function handleDeleteSession(sessionId) {
        if (!confirm("Are you sure you want to delete this session?")) return;

        try {
            const resp = await fetch("/api/sessions/" + sessionId, { method: "DELETE" });
            if (!resp.ok) throw new Error("Delete failed");

            if (currentSessionId === sessionId) {
                resetToEmptyState();
            }
            loadHistory();
        } catch (err) {
            alert("Could not delete session: " + err.message);
        }
    }

    // Append user message bubble
    function appendUserBubble(text) {
        const row = document.createElement("div");
        row.className = "message-row user";
        const bubble = document.createElement("div");
        bubble.className = "bubble user";
        bubble.textContent = text;
        row.appendChild(bubble);
        messageList.appendChild(row);
        scrollToBottom();
    }

    // Render assistant message content (parses JSON)
    function renderAssistantMessage(rawJson, sessionPrompts) {
        let parsed = null;
        try {
            parsed = typeof rawJson === "string" ? JSON.parse(rawJson) : rawJson;
        } catch (e) {
            parsed = null;
        }

        if (!parsed) {
            const row = document.createElement("div");
            row.className = "message-row assistant";
            const bubble = document.createElement("div");
            bubble.className = "bubble assistant";
            bubble.textContent = rawJson;
            row.appendChild(bubble);
            messageList.appendChild(row);
            scrollToBottom();
            return;
        }

        if (parsed.status === "ask" && parsed.question) {
            const row = document.createElement("div");
            row.className = "message-row assistant";
            const bubble = document.createElement("div");
            bubble.className = "bubble assistant";
            bubble.textContent = parsed.question;
            row.appendChild(bubble);
            messageList.appendChild(row);
        }

        if ((parsed.status === "ready" || parsed.final_prompt) && parsed.final_prompt) {
            const promptsList = (sessionPrompts && sessionPrompts.length > 0) ? sessionPrompts : (parsed.prompts || [parsed.final_prompt]);
            appendFinalPromptCard(parsed.final_prompt, promptsList, parsed.changes);
        }

        scrollToBottom();
    }

    // Append Final Prompt Card with Shorter, More detailed, Score, Version switcher, and Copy button
    function appendFinalPromptCard(initialPrompt, promptsList, initialChanges) {
        const prompts = Array.isArray(promptsList) && promptsList.length > 0 ? promptsList.slice() : [initialPrompt];
        let currentVersionIndex = prompts.length - 1;

        const card = document.createElement("div");
        card.className = "prompt-card";

        const header = document.createElement("div");
        header.className = "prompt-card-header";

        const title = document.createElement("div");
        title.className = "prompt-card-title";
        title.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line><polyline points="10 9 9 9 8 9"></polyline></svg> Final Prompt';

        const actions = document.createElement("div");
        actions.className = "prompt-card-actions";

        // Version switcher element
        const versionSwitcher = document.createElement("div");
        versionSwitcher.className = "version-switcher";

        const btnPrevVersion = document.createElement("button");
        btnPrevVersion.className = "version-btn";
        btnPrevVersion.setAttribute("aria-label", "Previous version");
        btnPrevVersion.textContent = "◀";

        const versionText = document.createElement("span");
        versionText.className = "version-text";

        const btnNextVersion = document.createElement("button");
        btnNextVersion.className = "version-btn";
        btnNextVersion.setAttribute("aria-label", "Next version");
        btnNextVersion.textContent = "▶";

        versionSwitcher.appendChild(btnPrevVersion);
        versionSwitcher.appendChild(versionText);
        versionSwitcher.appendChild(btnNextVersion);

        // Shorter button
        const btnShorter = document.createElement("button");
        btnShorter.className = "btn-refine";
        btnShorter.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="5" y1="12" x2="19" y2="12"></line></svg> <span>Shorter</span>';

        // More detailed button
        const btnDetailed = document.createElement("button");
        btnDetailed.className = "btn-refine";
        btnDetailed.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg> <span>More detailed</span>';

        // Score button
        const btnScore = document.createElement("button");
        btnScore.className = "btn-refine";
        btnScore.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"></polygon></svg> <span>Score</span>';

        // Copy button
        const btnCopy = document.createElement("button");
        btnCopy.className = "btn-copy";
        btnCopy.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg> <span>Copy</span>';

        // Monospace <pre> rendered strictly via textContent (avoids XSS)
        const pre = document.createElement("pre");
        pre.className = "prompt-content";

        function updateVersionView() {
            pre.textContent = prompts[currentVersionIndex];
            versionText.textContent = "v" + (currentVersionIndex + 1) + " of " + prompts.length;
            btnPrevVersion.disabled = currentVersionIndex <= 0;
            btnNextVersion.disabled = currentVersionIndex >= prompts.length - 1;
            versionSwitcher.style.display = prompts.length > 1 ? "inline-flex" : "none";
        }

        btnPrevVersion.addEventListener("click", function () {
            if (currentVersionIndex > 0) {
                currentVersionIndex--;
                updateVersionView();
            }
        });

        btnNextVersion.addEventListener("click", function () {
            if (currentVersionIndex < prompts.length - 1) {
                currentVersionIndex++;
                updateVersionView();
            }
        });

        // Refine request helper
        async function triggerRefine(mode, btnTrigger) {
            if (!currentSessionId || isLoading) return;
            const originalHtml = btnTrigger.innerHTML;
            btnTrigger.disabled = true;
            btnShorter.disabled = true;
            btnDetailed.disabled = true;
            btnScore.disabled = true;
            btnTrigger.innerHTML = '<span class="loading-text">Refining...</span>';

            try {
                const resp = await fetch("/api/sessions/" + currentSessionId + "/refine", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ mode: mode }),
                });
                const data = await resp.json();
                if (!resp.ok) {
                    throw new Error(data.error || "Failed to refine prompt");
                }

                prompts.push(data.final_prompt);
                currentVersionIndex = prompts.length - 1;
                updateVersionView();
                scrollToBottom();
            } catch (err) {
                alert("Refinement failed: " + err.message);
            } finally {
                btnTrigger.innerHTML = originalHtml;
                btnTrigger.disabled = false;
                btnShorter.disabled = false;
                btnDetailed.disabled = false;
                btnScore.disabled = false;
            }
        }

        btnShorter.addEventListener("click", function () {
            triggerRefine("shorter", btnShorter);
        });

        btnDetailed.addEventListener("click", function () {
            triggerRefine("detailed", btnDetailed);
        });

        // Score request handler
        btnScore.addEventListener("click", async function () {
            if (!currentSessionId || isLoading) return;
            const originalHtml = btnScore.innerHTML;
            btnScore.disabled = true;
            btnScore.innerHTML = '<span class="loading-text">Scoring...</span>';

            try {
                const resp = await fetch("/api/sessions/" + currentSessionId + "/score", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                });
                const data = await resp.json();
                if (!resp.ok) {
                    throw new Error(data.error || "Failed to score prompt");
                }

                // Remove any existing score panel in this card
                const existingPanel = card.querySelector(".score-panel");
                if (existingPanel) existingPanel.remove();

                // Create score panel
                const scorePanel = document.createElement("div");
                scorePanel.className = "score-panel";

                const scoreHeader = document.createElement("div");
                scoreHeader.className = "score-header";

                const overallSpan = document.createElement("div");
                overallSpan.className = "score-overall";
                overallSpan.innerHTML = '<strong>Prompt Score:</strong> <span class="score-badge">' + data.overall + '/10</span>';

                const metricsDiv = document.createElement("div");
                metricsDiv.className = "score-metrics";
                metricsDiv.innerHTML = '<span>Clarity: <strong>' + data.clarity + '/10</strong></span>' +
                                      '<span>Specificity: <strong>' + data.specificity + '/10</strong></span>' +
                                      '<span>Completeness: <strong>' + data.completeness + '/10</strong></span>';

                scoreHeader.appendChild(overallSpan);
                scoreHeader.appendChild(metricsDiv);
                scorePanel.appendChild(scoreHeader);

                if (Array.isArray(data.suggestions) && data.suggestions.length > 0) {
                    const suggestionsBox = document.createElement("div");
                    suggestionsBox.className = "score-suggestions";
                    suggestionsBox.innerHTML = '<div class="score-suggestions-title">Suggestions for improvement:</div>';
                    const ul = document.createElement("ul");
                    data.suggestions.forEach(function (s) {
                        const li = document.createElement("li");
                        li.textContent = s;
                        ul.appendChild(li);
                    });
                    suggestionsBox.appendChild(ul);
                    scorePanel.appendChild(suggestionsBox);
                }

                card.insertBefore(scorePanel, pre);
                scrollToBottom();
            } catch (err) {
                alert("Scoring failed: " + err.message);
            } finally {
                btnScore.innerHTML = originalHtml;
                btnScore.disabled = false;
            }
        });

        btnCopy.addEventListener("click", function () {
            copyToClipboard(prompts[currentVersionIndex], btnCopy);
        });

        actions.appendChild(versionSwitcher);
        actions.appendChild(btnShorter);
        actions.appendChild(btnDetailed);
        actions.appendChild(btnScore);
        actions.appendChild(btnCopy);

        header.appendChild(title);
        header.appendChild(actions);
        card.appendChild(header);

        // Changes list if present (Improve mode)
        if (Array.isArray(initialChanges) && initialChanges.length > 0) {
            const changesPanel = document.createElement("div");
            changesPanel.className = "changes-panel";
            changesPanel.innerHTML = '<div class="changes-title">What I changed:</div>';
            const ul = document.createElement("ul");
            ul.className = "changes-list";
            initialChanges.forEach(function (c) {
                const li = document.createElement("li");
                li.textContent = c;
                ul.appendChild(li);
            });
            changesPanel.appendChild(ul);
            card.appendChild(changesPanel);
        }

        card.appendChild(pre);
        messageList.appendChild(card);

        updateVersionView();
        scrollToBottom();
    }

    // Copy to clipboard with 1.5s "Copied!" feedback
    function copyToClipboard(text, btnElement) {
        if (!navigator.clipboard) {
            fallbackCopyTextToClipboard(text, btnElement);
            return;
        }

        navigator.clipboard.writeText(text).then(function () {
            showCopiedFeedback(btnElement);
        }).catch(function () {
            fallbackCopyTextToClipboard(text, btnElement);
        });
    }

    function fallbackCopyTextToClipboard(text, btnElement) {
        const textArea = document.createElement("textarea");
        textArea.value = text;
        textArea.style.position = "fixed";
        textArea.style.top = "0";
        textArea.style.left = "0";
        document.body.appendChild(textArea);
        textArea.focus();
        textArea.select();
        try {
            document.execCommand("copy");
            showCopiedFeedback(btnElement);
        } catch (err) {
            console.error("Fallback copy failed", err);
        }
        document.body.removeChild(textArea);
    }

    function showCopiedFeedback(btnElement) {
        btnElement.classList.add("copied");
        const span = btnElement.querySelector("span");
        const originalText = span ? span.textContent : "Copy";
        if (span) span.textContent = "Copied!";

        setTimeout(function () {
            btnElement.classList.remove("copied");
            if (span) span.textContent = originalText;
        }, 1500);
    }

    // Display error banner with retry option
    function showError(message, retryFn) {
        const errorRow = document.createElement("div");
        errorRow.className = "message-row assistant";

        const banner = document.createElement("div");
        banner.className = "error-bubble";

        const textSpan = document.createElement("span");
        textSpan.textContent = message;

        const btnRetry = document.createElement("button");
        btnRetry.className = "btn-retry";
        btnRetry.textContent = "Retry";
        btnRetry.addEventListener("click", function () {
            errorRow.remove();
            if (retryFn) retryFn();
        });

        banner.appendChild(textSpan);
        banner.appendChild(btnRetry);
        errorRow.appendChild(banner);
        messageList.appendChild(errorRow);
        scrollToBottom();
    }

    // Handle Send Action
    async function handleSend() {
        const text = inputText.value.trim();
        if (!text || isLoading) return;

        if (emptyState.style.display !== "none") {
            emptyState.style.display = "none";
            messageList.style.display = "flex";
        }

        appendUserBubble(text);
        inputText.value = "";
        autoResizeTextarea();
        setLoading(true);

        if (!currentSessionId) {
            if (isImproveMode) {
                // Improve prompt mode
                const action = async function () {
                    setLoading(true);
                    try {
                        const resp = await fetch("/api/improve", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ prompt: text }),
                        });
                        const data = await resp.json();
                        if (!resp.ok) {
                            throw new Error(data.error || "Failed to improve prompt");
                        }

                        currentSessionId = data.session_id;
                        currentSessionStatus = "ready";
                        renderAssistantMessage(data);
                        skipContainer.style.display = "none";
                        loadHistory();
                    } catch (err) {
                        showError(err.message, action);
                    } finally {
                        setLoading(false);
                    }
                };
                action();
            } else {
                // Standard session creation
                const action = async function () {
                    setLoading(true);
                    try {
                        const resp = await fetch("/api/sessions", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ idea: text }),
                        });

                        const data = await resp.json();
                        if (!resp.ok) {
                            throw new Error(data.error || "Failed to create session");
                        }

                        currentSessionId = data.session_id;
                        currentSessionStatus = data.status;

                        renderAssistantMessage(data);
                        skipContainer.style.display = currentSessionStatus === "asking" ? "flex" : "none";
                        loadHistory();
                    } catch (err) {
                        showError(err.message, action);
                    } finally {
                        setLoading(false);
                    }
                };
                action();
            }
        } else {
            // Advance existing session
            const action = async function () {
                setLoading(true);
                try {
                    const resp = await fetch("/api/sessions/" + currentSessionId + "/messages", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ text: text, skip: false }),
                    });

                    const data = await resp.json();
                    if (!resp.ok) {
                        throw new Error(data.error || "Failed to send message");
                    }

                    currentSessionStatus = data.status;
                    renderAssistantMessage(data);
                    skipContainer.style.display = currentSessionStatus === "asking" ? "flex" : "none";
                    loadHistory();
                } catch (err) {
                    showError(err.message, action);
                } finally {
                    setLoading(false);
                }
            };
            action();
        }
    }

    // Handle Skip Action
    async function handleSkip() {
        if (!currentSessionId || isLoading) return;

        const action = async function () {
            setLoading(true);
            try {
                const resp = await fetch("/api/sessions/" + currentSessionId + "/messages", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ skip: true }),
                });

                const data = await resp.json();
                if (!resp.ok) {
                    throw new Error(data.error || "Failed to generate prompt");
                }

                currentSessionStatus = data.status;
                renderAssistantMessage(data);
                skipContainer.style.display = "none";
                loadHistory();
            } catch (err) {
                showError(err.message, action);
            } finally {
                setLoading(false);
            }
        };
        action();
    }

    // Start application
    document.addEventListener("DOMContentLoaded", init);
})();
