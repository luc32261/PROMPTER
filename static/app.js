// Prompt Builder - Single Page Application
(function () {
    "use strict";

    // Wrap window.fetch to automatically include CSRF token for mutating requests
    const originalFetch = window.fetch;
    window.fetch = function (url, options = {}) {
        options = options || {};
        const method = (options.method || "GET").toUpperCase();
        if (["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
            options.headers = options.headers || {};
            const token = document.querySelector('meta[name="csrf-token"]')?.getAttribute("content");
            if (token) {
                if (options.headers instanceof Headers) {
                    if (!options.headers.has("X-CSRF-Token")) {
                        options.headers.set("X-CSRF-Token", token);
                    }
                } else if (Array.isArray(options.headers)) {
                    options.headers.push(["X-CSRF-Token", token]);
                } else {
                    if (!options.headers["X-CSRF-Token"]) {
                        options.headers["X-CSRF-Token"] = token;
                    }
                }
            }
        }
        return originalFetch.call(this, url, options);
    };

    // State
    let currentSessionId = null;
    let currentSessionStatus = null;
    let isLoading = false;
    let allSessions = [];
    let allTemplates = [];
    let activeFilter = "all";
    let activeTemplateCategory = "all";
    let currentPreviewTemplate = null;
    let searchQuery = "";
    let isImproveMode = false;
    let selectedAttachmentFile = null;

    // DOM Elements
    const emptyState = document.getElementById("empty-state");
    const messageList = document.getElementById("message-list");
    const chatContainer = document.getElementById("chat-container");
    const historyList = document.getElementById("history-list");
    const searchInput = document.getElementById("search-input");
    const inputText = document.getElementById("input-text");
    const btnSend = document.getElementById("btn-send");
    const btnAttach = document.getElementById("btn-attach");
    const fileInput = document.getElementById("file-input");
    const attachmentPreview = document.getElementById("attachment-preview");
    const attachmentName = document.getElementById("attachment-name");
    const btnRemoveAttachment = document.getElementById("btn-remove-attachment");
    const btnNewPrompt = document.getElementById("btn-new-prompt");
    const btnToggleMode = document.getElementById("btn-toggle-mode");
    const btnTemplates = document.getElementById("btn-templates");
    const mobileModeSelect = document.getElementById("mobile-mode-select");
    const mobileSegmentedControl = document.getElementById("mobile-segmented-control");
    const templatesModal = document.getElementById("templates-modal");
    const btnCloseTemplates = document.getElementById("btn-close-templates");
    const templatesListPane = document.getElementById("templates-list-pane");
    const templatePreviewPane = document.getElementById("template-preview-pane");
    const btnBackToList = document.getElementById("btn-back-to-list");
    const btnCopyTemplate = document.getElementById("btn-copy-template");
    const btnUseTemplate = document.getElementById("btn-use-template");
    const previewTemplateTitle = document.getElementById("preview-template-title");
    const previewTemplateBadge = document.getElementById("preview-template-badge");
    const previewTemplateBlurb = document.getElementById("preview-template-blurb");
    const previewTemplateContent = document.getElementById("preview-template-content");
    const skipContainer = document.getElementById("skip-container");
    const btnSkip = document.getElementById("btn-skip");
    const loadingIndicator = document.getElementById("loading-indicator");
    const btnToggleSidebar = document.getElementById("btn-toggle-sidebar");
    const sidebar = document.getElementById("sidebar");
    const sidebarOverlay = document.getElementById("sidebar-overlay");
    const appLayout = document.querySelector(".app-layout");
    const btnMinimizeSidebar = document.getElementById("btn-minimize-sidebar");
    const btnRestoreSidebar = document.getElementById("btn-restore-sidebar");
    const btnToggleFullscreen = document.getElementById("btn-toggle-fullscreen");
    const iconFsEnter = document.getElementById("icon-fs-enter");
    const iconFsExit = document.getElementById("icon-fs-exit");

    // Initialize
    function init() {
        bindEvents();
        loadHistory();
        resetToEmptyState();
        try {
            if (localStorage.getItem("sidebar_minimized") === "true" && window.innerWidth > 768) {
                toggleSidebarMinimize(true);
            }
        } catch (e) {}
    }

    // Bind UI Event Listeners
    function bindEvents() {
        btnNewPrompt.addEventListener("click", function () {
            closeTemplatesModal();
            isImproveMode = false;
            updateModeUI("new");
            resetToEmptyState();
        });

        if (btnToggleMode) {
            btnToggleMode.addEventListener("click", function () {
                closeTemplatesModal();
                isImproveMode = !isImproveMode;
                updateModeUI(isImproveMode ? "improve" : "new");
                resetToEmptyState();
            });
        }

        if (btnTemplates) {
            btnTemplates.addEventListener("click", function () {
                openTemplatesModal();
            });
        }

        if (mobileModeSelect) {
            mobileModeSelect.addEventListener("change", function () {
                const mode = mobileModeSelect.value;
                if (mode === "templates") {
                    openTemplatesModal();
                } else if (mode === "improve") {
                    closeTemplatesModal();
                    isImproveMode = true;
                    updateModeUI("improve");
                    resetToEmptyState();
                } else {
                    closeTemplatesModal();
                    isImproveMode = false;
                    updateModeUI("new");
                    resetToEmptyState();
                }
            });
        }

        if (mobileSegmentedControl) {
            mobileSegmentedControl.querySelectorAll(".segment-btn").forEach(function (btn) {
                btn.addEventListener("click", function () {
                    const mode = btn.getAttribute("data-mode");
                    if (mode === "templates") {
                        openTemplatesModal();
                    } else if (mode === "improve") {
                        closeTemplatesModal();
                        isImproveMode = true;
                        updateModeUI("improve");
                        resetToEmptyState();
                    } else {
                        closeTemplatesModal();
                        isImproveMode = false;
                        updateModeUI("new");
                        resetToEmptyState();
                    }
                });
            });
        }

        if (btnCloseTemplates) {
            btnCloseTemplates.addEventListener("click", closeTemplatesModal);
        }

        if (templatesModal) {
            templatesModal.addEventListener("click", function (e) {
                if (e.target === templatesModal) {
                    closeTemplatesModal();
                }
            });
        }

        if (btnBackToList) {
            btnBackToList.addEventListener("click", function () {
                if (templatePreviewPane) templatePreviewPane.style.display = "none";
                if (templatesListPane) templatesListPane.style.display = "flex";
                currentPreviewTemplate = null;
            });
        }

        if (btnCopyTemplate) {
            btnCopyTemplate.addEventListener("click", function () {
                if (currentPreviewTemplate) {
                    copyToClipboard(currentPreviewTemplate.content, btnCopyTemplate);
                }
            });
        }

        if (btnUseTemplate) {
            btnUseTemplate.addEventListener("click", useTemplateAsStartingPoint);
        }

        // Templates category chips
        document.querySelectorAll(".tpl-chip").forEach(function (chip) {
            chip.addEventListener("click", function () {
                document.querySelectorAll(".tpl-chip").forEach(function (c) {
                    c.classList.remove("active");
                });
                chip.classList.add("active");
                activeTemplateCategory = chip.getAttribute("data-category") || "all";
                renderTemplatesList();
            });
        });

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
            // Escape to close templates modal
            if (e.key === "Escape" && templatesModal && templatesModal.classList.contains("active")) {
                closeTemplatesModal();
            }
            // Ctrl+B / Cmd+B to toggle / minimize sidebar window
            if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "b") {
                e.preventDefault();
                toggleSidebarMinimize();
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

        if (btnAttach && fileInput) {
            btnAttach.addEventListener("click", function () {
                fileInput.click();
            });
            fileInput.addEventListener("change", handleFileSelected);
        }

        if (btnRemoveAttachment) {
            btnRemoveAttachment.addEventListener("click", clearSelectedAttachment);
        }

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

        // Admin link navigation
        const btnAdminLink = document.getElementById("btn-admin-link");
        if (btnAdminLink) {
            btnAdminLink.addEventListener("click", async function (e) {
                e.preventDefault();
                // If page rendered when admin was not active, go directly to gate
                if (btnAdminLink.dataset.adminActive === "false") {
                    window.location.href = "/admin/gate";
                    return;
                }
                // If it was active, verify with /api/admin/check to handle session expiry
                try {
                    const resp = await fetch("/api/admin/check");
                    if (resp.ok) {
                        window.location.href = "/admin";
                    } else {
                        window.location.href = "/admin/gate";
                    }
                } catch (err) {
                    window.location.href = "/admin/gate";
                }
            });
        }

        // Minimize and restore sidebar window
        if (btnMinimizeSidebar) {
            btnMinimizeSidebar.addEventListener("click", function () {
                if (window.innerWidth <= 768) {
                    if (sidebar) sidebar.classList.remove("open");
                    if (sidebarOverlay) sidebarOverlay.classList.remove("active");
                } else {
                    toggleSidebarMinimize(true);
                }
            });
        }

        if (btnRestoreSidebar) {
            btnRestoreSidebar.addEventListener("click", function () {
                toggleSidebarMinimize(false);
            });
        }

        if (btnToggleFullscreen) {
            btnToggleFullscreen.addEventListener("click", function () {
                if (!document.fullscreenElement) {
                    if (document.documentElement.requestFullscreen) {
                        document.documentElement.requestFullscreen().catch(function () {});
                    }
                } else {
                    if (document.exitFullscreen) {
                        document.exitFullscreen().catch(function () {});
                    }
                }
            });

            document.addEventListener("fullscreenchange", function () {
                const isFs = !!document.fullscreenElement;
                if (iconFsEnter) iconFsEnter.style.display = isFs ? "none" : "block";
                if (iconFsExit) iconFsExit.style.display = isFs ? "block" : "none";
            });
        }
    }

    // Toggle Minimize Sidebar Window
    function toggleSidebarMinimize(forceState) {
        if (!appLayout) return;
        const isMin = typeof forceState === "boolean"
            ? forceState
            : !appLayout.classList.contains("sidebar-minimized");

        appLayout.classList.toggle("sidebar-minimized", isMin);
        try {
            localStorage.setItem("sidebar_minimized", isMin ? "true" : "false");
        } catch (e) {}
    }

    // Attachment handlers
    function handleFileSelected() {
        if (!fileInput.files || fileInput.files.length === 0) return;
        const file = fileInput.files[0];
        const MAX_SIZE = 10 * 1024 * 1024; // 10MB
        if (file.size > MAX_SIZE) {
            alert("File size exceeds 10MB limit. Please choose a smaller file.");
            clearSelectedAttachment();
            return;
        }

        const validExts = [".pdf", ".docx", ".txt"];
        const lowerName = file.name.toLowerCase();
        const hasValidExt = validExts.some(function (ext) { return lowerName.endsWith(ext); });
        if (!hasValidExt) {
            alert("Unsupported file format. Please upload a .pdf, .docx, or .txt file.");
            clearSelectedAttachment();
            return;
        }

        selectedAttachmentFile = file;
        if (attachmentName) attachmentName.textContent = file.name;
        if (attachmentPreview) attachmentPreview.style.display = "flex";
        if (inputText) inputText.focus();
    }

    function clearSelectedAttachment() {
        selectedAttachmentFile = null;
        if (fileInput) fileInput.value = "";
        if (attachmentPreview) attachmentPreview.style.display = "none";
        if (attachmentName) attachmentName.textContent = "";
    }

    function escapeHtml(str) {
        if (!str) return "";
        return String(str)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    // Update Mode UI state
    function updateModeUI(mode) {
        if (mode === "improve") {
            isImproveMode = true;
        } else if (mode === "new") {
            isImproveMode = false;
        }

        if (btnToggleMode) {
            btnToggleMode.classList.toggle("active", isImproveMode);
            btnToggleMode.setAttribute("aria-pressed", isImproveMode ? "true" : "false");
        }

        if (mobileModeSelect) {
            mobileModeSelect.value = isImproveMode ? "improve" : "new";
        }

        if (mobileSegmentedControl) {
            mobileSegmentedControl.querySelectorAll(".segment-btn").forEach(function (btn) {
                btn.classList.toggle("active", btn.getAttribute("data-mode") === (isImproveMode ? "improve" : "new"));
            });
        }

        const emptyTitle = emptyState.querySelector("h2");
        const emptyDesc = emptyState.querySelector("p");

        if (isImproveMode) {
            clearSelectedAttachment();
            if (btnAttach) btnAttach.style.display = "none";
            if (emptyTitle) emptyTitle.textContent = "Improve an existing prompt";
            if (emptyDesc) emptyDesc.textContent = "Paste any draft or existing prompt below. The assistant will transform it into a structured, model-agnostic prompt and detail the changes made.";
            inputText.placeholder = "Paste your existing prompt here to improve it... (Ctrl+K to focus, Enter to send)";
        } else {
            if (btnAttach && !currentSessionId) btnAttach.style.display = "inline-flex";
            if (emptyTitle) emptyTitle.textContent = "What do you want a prompt for?";
            if (emptyDesc) emptyDesc.textContent = "Enter a rough idea below. The assistant will ask a few quick questions to craft an optimal, model-agnostic prompt.";
            inputText.placeholder = "Type your idea or answer... (Ctrl+K to focus, Enter to send)";
        }
    }

    // Templates UI Handlers
    function openTemplatesModal() {
        if (!templatesModal) return;
        templatesModal.classList.add("active");
        if (mobileModeSelect) mobileModeSelect.value = "templates";
        if (mobileSegmentedControl) {
            mobileSegmentedControl.querySelectorAll(".segment-btn").forEach(function (btn) {
                btn.classList.toggle("active", btn.getAttribute("data-mode") === "templates");
            });
        }
        if (templatePreviewPane) templatePreviewPane.style.display = "none";
        if (templatesListPane) templatesListPane.style.display = "flex";
        currentPreviewTemplate = null;
        loadTemplates();
    }

    function closeTemplatesModal() {
        if (!templatesModal) return;
        templatesModal.classList.remove("active");
        if (mobileModeSelect) mobileModeSelect.value = isImproveMode ? "improve" : "new";
        if (mobileSegmentedControl) {
            mobileSegmentedControl.querySelectorAll(".segment-btn").forEach(function (btn) {
                btn.classList.toggle("active", btn.getAttribute("data-mode") === (isImproveMode ? "improve" : "new"));
            });
        }
    }

    async function loadTemplates() {
        if (!templatesListPane) return;
        try {
            const resp = await fetch("/api/templates");
            if (resp.status === 401) {
                window.location.href = "/login";
                return;
            }
            if (!resp.ok) {
                throw new Error("Failed to load templates");
            }
            allTemplates = await resp.json();
            renderTemplatesList();
        } catch (err) {
            templatesListPane.innerHTML = `<div class="templates-empty-state"><p style="color:var(--danger)">${err.message}</p></div>`;
        }
    }

    function renderTemplatesList() {
        if (!templatesListPane) return;
        templatesListPane.innerHTML = "";

        const filtered = allTemplates.filter(function (t) {
            if (activeTemplateCategory === "all") return true;
            return t.category === activeTemplateCategory;
        });

        if (filtered.length === 0) {
            const emptyDiv = document.createElement("div");
            emptyDiv.className = "templates-empty-state";
            emptyDiv.innerHTML = `
                <div class="templates-empty-icon">
                    <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"></path>
                        <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"></path>
                    </svg>
                </div>
                <h3>No templates yet</h3>
                <p>${activeTemplateCategory === "all" ? "No prompt templates have been added yet. Check back soon!" : "No templates found in this category."}</p>
            `;
            templatesListPane.appendChild(emptyDiv);
            return;
        }

        const categories = activeTemplateCategory === "all"
            ? ["study", "writing", "research", "other"]
            : [activeTemplateCategory];

        categories.forEach(function (cat) {
            const catTemplates = filtered.filter(function (t) {
                return t.category === cat;
            });
            if (catTemplates.length === 0) return;

            const groupDiv = document.createElement("div");
            groupDiv.className = "template-category-group";

            const titleSpan = document.createElement("span");
            titleSpan.className = "template-category-title";
            titleSpan.textContent = cat.charAt(0).toUpperCase() + cat.slice(1);
            groupDiv.appendChild(titleSpan);

            const gridDiv = document.createElement("div");
            gridDiv.className = "templates-grid";

            catTemplates.forEach(function (tpl) {
                const card = document.createElement("div");
                card.className = "template-card";
                card.setAttribute("data-id", tpl.id);

                const cardTop = document.createElement("div");
                cardTop.className = "template-card-top";

                const cardTitle = document.createElement("span");
                cardTitle.className = "template-card-title";
                cardTitle.textContent = tpl.title;

                const badge = document.createElement("span");
                badge.className = "type-badge " + tpl.category;
                badge.textContent = tpl.category;

                cardTop.appendChild(cardTitle);
                cardTop.appendChild(badge);

                const blurb = document.createElement("p");
                blurb.className = "template-card-blurb";
                blurb.textContent = tpl.blurb || "";

                card.appendChild(cardTop);
                card.appendChild(blurb);

                card.addEventListener("click", function () {
                    showTemplatePreview(tpl.id);
                });

                gridDiv.appendChild(card);
            });

            groupDiv.appendChild(gridDiv);
            templatesListPane.appendChild(groupDiv);
        });
    }

    async function showTemplatePreview(templateId) {
        if (!templatesListPane || !templatePreviewPane) return;
        try {
            const resp = await fetch("/api/templates/" + templateId);
            if (resp.status === 401) {
                window.location.href = "/login";
                return;
            }
            if (!resp.ok) {
                throw new Error("Failed to load template details");
            }
            const tpl = await resp.json();
            currentPreviewTemplate = tpl;

            if (previewTemplateTitle) previewTemplateTitle.textContent = tpl.title;
            if (previewTemplateBadge) {
                previewTemplateBadge.textContent = tpl.category;
                previewTemplateBadge.className = "type-badge " + tpl.category;
            }
            if (previewTemplateBlurb) previewTemplateBlurb.textContent = tpl.blurb || "";
            if (previewTemplateContent) previewTemplateContent.textContent = tpl.content || "";

            templatesListPane.style.display = "none";
            templatePreviewPane.style.display = "flex";
        } catch (err) {
            showError(err.message);
        }
    }

    async function useTemplateAsStartingPoint() {
        if (!currentPreviewTemplate || isLoading) return;
        setLoading(true);
        try {
            const resp = await fetch("/api/templates/" + currentPreviewTemplate.id + "/use", {
                method: "POST",
                headers: { "Content-Type": "application/json" }
            });
            if (resp.status === 401) {
                window.location.href = "/login";
                return;
            }
            const data = await resp.json();
            if (!resp.ok) {
                throw new Error(data.error || "Failed to use template");
            }

            closeTemplatesModal();
            await loadSession(data.session_id);
            await loadHistory();
        } catch (err) {
            showError(err.message);
        } finally {
            setLoading(false);
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
        clearSelectedAttachment();
        if (btnAttach) btnAttach.style.display = isImproveMode ? "none" : "inline-flex";
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
            if (resp.status === 401) {
                window.location.href = "/login";
                return;
            }
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

            if (sess.has_attachment) {
                const attIcon = document.createElement("span");
                attIcon.className = "history-att-icon";
                attIcon.title = sess.attachment_filename || "Has attachment";
                attIcon.style.display = "inline-flex";
                attIcon.style.alignItems = "center";
                attIcon.style.color = "var(--accent-primary)";
                attIcon.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"></path></svg>';
                meta.appendChild(attIcon);
            }

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
            if (resp.status === 401) {
                window.location.href = "/login";
                return;
            }
            if (!resp.ok) {
                throw new Error("Failed to load session");
            }
            const data = await resp.json();
            currentSessionId = data.id;
            currentSessionStatus = data.status;
            clearSelectedAttachment();
            if (btnAttach) btnAttach.style.display = "none";

            emptyState.style.display = "none";
            messageList.style.display = "flex";
            messageList.innerHTML = "";

            // Render messages
            let firstUserMsg = true;
            if (data.messages && data.messages.length > 0) {
                data.messages.forEach(function (msg) {
                    if (msg.role === "user") {
                        const att = (firstUserMsg && data.has_attachment && data.attachment_filename) ? data.attachment_filename : null;
                        appendUserBubble(msg.content, att);
                        firstUserMsg = false;
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

    // Append user message bubble with optional attachment chip
    function appendUserBubble(text, attachmentFilename) {
        const row = document.createElement("div");
        row.className = "message-row user";
        const bubble = document.createElement("div");
        bubble.className = "bubble user";

        if (attachmentFilename) {
            const chip = document.createElement("div");
            chip.className = "attachment-chat-chip";
            chip.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"></path></svg> <span>' + escapeHtml(attachmentFilename) + '</span>';
            bubble.appendChild(chip);
        }

        const textDiv = document.createElement("div");
        textDiv.className = "user-text-content";
        textDiv.textContent = text;
        bubble.appendChild(textDiv);

        row.appendChild(bubble);
        messageList.appendChild(row);
        scrollToBottom();
    }

    // Render assistant message content (parses JSON)
    function renderAssistantMessage(rawJson, sessionPrompts) {
        let parsed = null;
        if (typeof rawJson === "object" && rawJson !== null) {
            parsed = rawJson;
        } else if (typeof rawJson === "string") {
            try {
                parsed = JSON.parse(rawJson);
            } catch (e) {
                const match = rawJson.match(/(\{[\s\S]*\})/);
                if (match) {
                    try {
                        parsed = JSON.parse(match[1]);
                    } catch (e2) {
                        parsed = null;
                    }
                }
            }
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
                if (err.message === "Daily limit reached") {
                    alert("You have reached your daily limit of LLM calls. Your limit will reset at 00:00 UTC.");
                } else {
                    alert("Refinement failed: " + err.message);
                }
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
                if (err.message === "Daily limit reached") {
                    alert("You have reached your daily limit of LLM calls. Your limit will reset at 00:00 UTC.");
                } else {
                    alert("Scoring failed: " + err.message);
                }
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

        let friendlyMessage = message;
        const isDailyLimit = message === "Daily limit reached";
        if (isDailyLimit) {
            friendlyMessage = "You have reached your daily limit of LLM calls. Your limit will reset at 00:00 UTC.";
        }

        const textSpan = document.createElement("span");
        textSpan.textContent = friendlyMessage;

        const btnRetry = document.createElement("button");
        btnRetry.className = "btn-retry";
        btnRetry.textContent = "Retry";
        btnRetry.addEventListener("click", function () {
            errorRow.remove();
            if (retryFn) retryFn();
        });

        banner.appendChild(textSpan);
        if (!isDailyLimit) {
            banner.appendChild(btnRetry);
        }
        errorRow.appendChild(banner);
        messageList.appendChild(errorRow);
        scrollToBottom();
    }

    // Handle Send Action
    async function handleSend() {
        const text = inputText.value.trim();
        const file = selectedAttachmentFile;
        if ((!text && !file) || isLoading) return;

        if (emptyState.style.display !== "none") {
            emptyState.style.display = "none";
            messageList.style.display = "flex";
        }

        const displayText = text || (file ? `Attached: ${file.name}` : "");
        const displayAttachment = file ? file.name : null;

        appendUserBubble(displayText, displayAttachment);
        inputText.value = "";
        autoResizeTextarea();
        clearSelectedAttachment();
        if (btnAttach) btnAttach.style.display = "none";
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
                        if (resp.status === 401) {
                            window.location.href = "/login";
                            return;
                        }
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
                        let resp;
                        if (file) {
                            const formData = new FormData();
                            formData.append("idea", text || `Document: ${file.name}`);
                            formData.append("file", file);
                            resp = await fetch("/api/sessions", {
                                method: "POST",
                                body: formData,
                            });
                        } else {
                            resp = await fetch("/api/sessions", {
                                method: "POST",
                                headers: { "Content-Type": "application/json" },
                                body: JSON.stringify({ idea: text }),
                            });
                        }
                        if (resp.status === 401) {
                            window.location.href = "/login";
                            return;
                        }

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
                    if (resp.status === 401) {
                        window.location.href = "/login";
                        return;
                    }

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
                if (resp.status === 401) {
                    window.location.href = "/login";
                    return;
                }

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
