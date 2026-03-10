// PEXA Notification Tracker - Frontend

const STAFF_LIST = [
    { name: "Jai", email: "jai@legalworld.com.au" },
    { name: "Sheriff", email: "sheriff@legalworld.com.au" },
    { name: "Zane", email: "zane@legalworld.com.au" },
    { name: "Kavya", email: "kavya@legalworld.com.au" },
    { name: "Shreya", email: "shreya@legalworld.com.au" },
    { name: "Thaanya", email: "thaanya@legalworld.com.au" },
    { name: "Settlements", email: "settlements@legalworld.com.au" },
];

const state = {
    notifications: [],
    selectedIds: new Set(),
    expandedId: null,
    filters: { hide_closed: "true" },
    currentUser: localStorage.getItem("pexaUser") || "Jai",
    sortField: "received_at",
    sortDir: "desc",
    autoRefreshInterval: null,
    emailModalNotification: null,
    nextSyncTime: null,
    countdownInterval: null,
};

// --- API Calls ---

async function fetchNotifications() {
    const params = new URLSearchParams();
    for (const [key, val] of Object.entries(state.filters)) {
        if (val) params.set(key, val);
    }
    const resp = await fetch(`/api/notifications?${params}`);
    state.notifications = await resp.json();
    renderTable();
}

async function fetchStats() {
    const resp = await fetch("/api/stats");
    const stats = await resp.json();
    renderStats(stats);
}

async function syncNow() {
    const btn = document.getElementById("btn-sync");
    btn.disabled = true;
    btn.classList.add("syncing");
    btn.textContent = "Syncing...";
    try {
        const resp = await fetch("/api/sync", { method: "POST" });
        const data = await resp.json();
        if (data.success) {
            showToast(`Sync complete: ${data.new_count} new notifications`, "success");
        } else {
            showToast(`Sync failed: ${data.status}`, "error");
        }
        await refreshAll();
    } catch (e) {
        showToast(`Sync error: ${e.message}`, "error");
    } finally {
        btn.disabled = false;
        btn.classList.remove("syncing");
        btn.textContent = "Sync Now";
    }
}

async function updateStatus(id, status) {
    await fetch(`/api/notifications/${id}/status`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status, user: state.currentUser }),
    });
    showToast(`Marked as ${status}`, "success");
    await refreshAll();
}

async function addNote(id) {
    const input = document.getElementById(`note-input-${id}`);
    const text = input.value.trim();
    if (!text) return;
    await fetch(`/api/notifications/${id}/note`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note: text, user: state.currentUser }),
    });
    input.value = "";
    showToast("Note added", "success");
    await refreshAll();
}

// --- Email Task ---

// Track selected recipients for the email modal
let emailRecipients = [];

function addEmailRecipient(email, name) {
    email = email.trim();
    if (!email) return;
    // Avoid duplicates
    if (emailRecipients.some(r => r.email === email)) return;
    emailRecipients.push({ email, name: name || email });
    renderRecipientChips();
}

function removeEmailRecipient(email) {
    emailRecipients = emailRecipients.filter(r => r.email !== email);
    renderRecipientChips();
}

function renderRecipientChips() {
    const container = document.getElementById("recipient-chips");
    if (!container) return;
    container.innerHTML = emailRecipients.map(r =>
        `<span class="recipient-chip">${escapeHtml(r.name)} <button type="button" onclick="removeEmailRecipient('${r.email}')">&times;</button></span>`
    ).join("");
}

function openEmailModal(id) {
    const n = state.notifications.find(x => x.id === id);
    if (!n) return;
    state.emailModalNotification = n;
    emailRecipients = [];

    const staffOptions = STAFF_LIST.map(s =>
        `<option value="${s.email}" data-name="${s.name}">${s.name} (${s.email})</option>`
    ).join("");

    const defaultMessage = `Hi,\n\nPlease action the following PEXA notification:\n\n` +
        `Matter #: ${n.matter_number}\n` +
        `Type: ${n.notification_type}\n` +
        `Settlement: ${n.settlement_date || "N/A"}\n` +
        `Summary: ${n.summary}\n\n` +
        `--- Full PEXA Message ---\n\n` +
        `${n.full_body || "No content available"}\n\n` +
        `--- End of Message ---\n\n` +
        `Please complete and update the tracker when done.\n\nThanks,\n${state.currentUser}`;

    document.getElementById("email-modal").innerHTML = `
        <div class="modal-backdrop" onclick="closeEmailModal()"></div>
        <div class="modal-content">
            <div class="modal-header">
                <h3>Send Task Email</h3>
                <button class="modal-close" onclick="closeEmailModal()">&times;</button>
            </div>
            <div class="modal-body">
                <div class="form-group">
                    <label>To <span style="font-weight:normal;color:#888;font-size:12px">(select multiple from list and/or type emails)</span></label>
                    <div id="recipient-chips" class="recipient-chips"></div>
                    <div style="display:flex;gap:6px;margin-top:4px">
                        <select id="email-to-select" class="form-control" style="flex:1" onchange="onStaffSelect()">
                            <option value="">+ Add from staff list...</option>
                            ${staffOptions}
                        </select>
                    </div>
                    <div style="display:flex;gap:6px;margin-top:6px">
                        <input type="email" id="email-to-custom" class="form-control" placeholder="Type email address and press Add..." style="flex:1"
                            onkeydown="if(event.key==='Enter'){event.preventDefault();onAddCustomEmail();}">
                        <button type="button" class="btn btn-review" onclick="onAddCustomEmail()" style="white-space:nowrap;padding:6px 14px">Add</button>
                    </div>
                </div>
                <div class="form-group">
                    <label>Subject</label>
                    <input type="text" id="email-subject" class="form-control"
                        value="${isSettlementTodayOrTomorrow(n.settlement_date) ? 'URGENT - ' : ''}${n.matter_number} - Settlement Date ${formatSettlementDateOnly(n.settlement_date)} - PEXA Action Required">
                </div>
                <div class="form-group">
                    <label>Message</label>
                    <textarea id="email-message" class="form-control" rows="16">${defaultMessage}</textarea>
                </div>
            </div>
            <div class="modal-footer">
                <button class="btn btn-dismiss" onclick="closeEmailModal()">Cancel</button>
                <button class="btn btn-send" onclick="sendTaskEmail(${n.id})">Send Email</button>
            </div>
        </div>`;
    document.getElementById("email-modal").classList.add("open");
}

function onStaffSelect() {
    const sel = document.getElementById("email-to-select");
    if (!sel.value) return;
    const opt = sel.options[sel.selectedIndex];
    addEmailRecipient(sel.value, opt.dataset.name || sel.value);
    sel.value = "";
}

function onAddCustomEmail() {
    const input = document.getElementById("email-to-custom");
    const email = input.value.trim();
    if (!email) return;
    // Basic email validation
    if (!email.includes("@")) {
        showToast("Please enter a valid email address", "error");
        return;
    }
    addEmailRecipient(email);
    input.value = "";
}

function closeEmailModal() {
    document.getElementById("email-modal").classList.remove("open");
    document.getElementById("email-modal").innerHTML = "";
    state.emailModalNotification = null;
    emailRecipients = [];
}

async function sendTaskEmail(notificationId) {
    if (emailRecipients.length === 0) {
        showToast("Please add at least one recipient", "error");
        return;
    }

    const toEmails = emailRecipients.map(r => r.email);
    const subject = document.getElementById("email-subject").value;
    const message = document.getElementById("email-message").value;

    const sendBtn = document.querySelector(".btn-send");
    sendBtn.disabled = true;
    sendBtn.textContent = "Sending...";

    try {
        const resp = await fetch("/api/send-task", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                notification_id: notificationId,
                to_email: toEmails,
                subject: subject,
                message: message,
                from_user: state.currentUser,
            }),
        });
        const data = await resp.json();
        if (data.success) {
            showToast(`Task emailed to ${toEmails.join(", ")}`, "success");
            closeEmailModal();
            await refreshAll();
        } else {
            showToast(`Failed to send: ${data.error}`, "error");
        }
    } catch (e) {
        showToast(`Send error: ${e.message}`, "error");
    } finally {
        sendBtn.disabled = false;
        sendBtn.textContent = "Send Email";
    }
}

async function bulkAction(action) {
    if (state.selectedIds.size === 0) return;
    await fetch("/api/bulk-action", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            ids: Array.from(state.selectedIds),
            action,
            user: state.currentUser,
        }),
    });
    state.selectedIds.clear();
    showToast(`${action} applied to selected items`, "success");
    await refreshAll();
}

async function checkConnection() {
    try {
        const resp = await fetch("/api/connection");
        const data = await resp.json();
        const banner = document.getElementById("connection-banner");
        if (data.connected) {
            banner.className = "connection-banner connected";
            banner.innerHTML = `Connected to <strong>${data.mailbox}</strong> - Folder: "${data.folder}" (${data.total_items} emails, ${data.unread_items} unread)`;
        } else {
            banner.className = "connection-banner error";
            banner.innerHTML = `Not connected: ${data.error}. Check your .env configuration.`;
        }
    } catch (e) {
        const banner = document.getElementById("connection-banner");
        banner.className = "connection-banner error";
        banner.innerHTML = `Cannot reach server: ${e.message}`;
    }
}

// --- Rendering ---

function renderStats(stats) {
    document.getElementById("stat-action").textContent = stats.action_required || 0;
    document.getElementById("stat-review").textContent = stats.review || 0;
    document.getElementById("stat-info").textContent = stats.info || 0;
    document.getElementById("stat-done").textContent = stats.completed || 0;
    document.getElementById("stat-new").textContent = stats.new || 0;

    const syncEl = document.getElementById("sync-info");
    if (stats.last_sync_time) {
        // Server sends Sydney time - display it directly
        const syncTime = new Date(stats.last_sync_time);
        const timeStr = syncTime.toLocaleTimeString("en-AU", {
            hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: true,
            timeZone: "Australia/Sydney"
        });
        syncEl.innerHTML = `Last sync: ${timeStr} - ${stats.last_sync} | <span id="countdown" class="countdown"></span>`;
    } else {
        syncEl.innerHTML = stats.last_sync || "Never synced";
    }

    // Set up countdown timer
    if (stats.next_sync_time) {
        state.nextSyncTime = new Date(stats.next_sync_time);
        startCountdown();
    }
}

function startCountdown() {
    // Clear any existing countdown
    if (state.countdownInterval) {
        clearInterval(state.countdownInterval);
    }
    updateCountdown();
    state.countdownInterval = setInterval(updateCountdown, 1000);
}

function updateCountdown() {
    const el = document.getElementById("countdown");
    if (!el || !state.nextSyncTime) return;

    const now = new Date();
    // Convert current time to Sydney for comparison
    const nowSydney = new Date(now.toLocaleString("en-US", { timeZone: "Australia/Sydney" }));
    const nextSydney = new Date(state.nextSyncTime);
    const diffMs = nextSydney - nowSydney;

    if (diffMs <= 0) {
        el.textContent = "Syncing soon...";
        return;
    }

    const mins = Math.floor(diffMs / 60000);
    const secs = Math.floor((diffMs % 60000) / 1000);
    el.textContent = `Next sync: ${mins}m ${secs.toString().padStart(2, "0")}s`;
}

function renderTable() {
    const tbody = document.getElementById("notifications-body");
    const filtered = applyQuickFilter(state.notifications);
    const notifications = sortNotifications(filtered);

    if (notifications.length === 0) {
        tbody.innerHTML = `
            <tr><td colspan="10" class="empty-state">
                <div class="empty-icon">📭</div>
                <h3>No notifications found</h3>
                <p>Try adjusting your filters or sync to check for new emails</p>
            </td></tr>`;
        return;
    }

    tbody.innerHTML = notifications.map(n => {
        const isExpanded = state.expandedId === n.id;
        const isSelected = state.selectedIds.has(n.id);

        return `
            <tr class="${isSelected ? "selected" : ""} ${isExpanded ? "expanded" : ""}"
                onclick="toggleExpand(${n.id}, event)">
                <td class="checkbox-cell" data-label="" onclick="event.stopPropagation()">
                    <input type="checkbox" ${isSelected ? "checked" : ""}
                        onchange="toggleSelect(${n.id}, this.checked)">
                </td>
                <td data-label="Matter #"><span class="matter-num">${escapeHtml(n.matter_number)}</span></td>
                <td data-label="Priority">${renderPriority(n.category)}</td>
                <td data-label="Type">${escapeHtml(n.notification_type)}</td>
                <td data-label="From" class="from-cell" title="${escapeHtml(n.message_from || '')}">${truncate(n.message_from || '', 25)}</td>
                <td data-label="Summary" title="${escapeHtml(n.summary)}">${truncate(n.summary, 60)}</td>
                <td data-label="Settlement">${formatDate(n.settlement_date)}</td>
                <td data-label="Received">${formatDateTime(n.received_at)}</td>
                <td data-label="Emailed To" class="emailed-to-cell" title="${escapeHtml(n.emailed_to || '')}">${renderEmailedTo(n.emailed_to)}</td>
                <td data-label="Status">${renderStatus(n.status)}</td>
            </tr>
            <tr class="detail-panel ${isExpanded ? "open" : ""}" id="detail-${n.id}">
                <td colspan="10">
                    ${isExpanded ? renderDetail(n) : ""}
                </td>
            </tr>`;
    }).join("");

    updateBulkActions();
}

function renderPriority(category) {
    const labels = {
        action_required: "Action",
        review: "Review",
        info: "Info",
    };
    return `<span class="priority-badge priority-${category === 'action_required' ? 'action' : category}">
        <span class="priority-dot"></span>
        ${labels[category] || category}
    </span>`;
}

function renderEmailedTo(emailedTo) {
    if (!emailedTo) return `<span style="color:#aaa">—</span>`;
    // Show short names where possible, full emails otherwise
    const emails = emailedTo.split(",").map(e => e.trim()).filter(Boolean);
    const display = emails.map(email => {
        const staff = STAFF_LIST.find(s => s.email === email);
        return staff ? staff.name : email;
    });
    return `<span class="emailed-to-names">${escapeHtml(display.join(", "))}</span>`;
}

function renderStatus(status) {
    const labels = {
        new: "New",
        reviewed: "Reviewed",
        actioned: "Actioned",
        dismissed: "Dismissed",
    };
    return `<span class="status-badge status-${status}">${labels[status] || status}</span>`;
}

function renderDetail(n) {
    return `
        <div class="detail-content">
            <div class="detail-section">
                <h4>Notification Details</h4>
                <div class="detail-field">
                    <div class="field-label">Matter Number</div>
                    <div class="field-value">${escapeHtml(n.matter_number)}</div>
                </div>
                <div class="detail-field">
                    <div class="field-label">Settlement Date</div>
                    <div class="field-value ${isSettlementSoon(n.settlement_date) ? 'settlement-soon' : ''}">${escapeHtml(n.settlement_date || "N/A")}</div>
                </div>
                <div class="detail-field">
                    <div class="field-label">Workspace</div>
                    <div class="field-value">${escapeHtml(n.workspace_number || "N/A")} (${escapeHtml(n.workspace_status || "N/A")})</div>
                </div>
                ${n.message_from ? `
                <div class="detail-field">
                    <div class="field-label">From</div>
                    <div class="field-value">${escapeHtml(n.message_from)}</div>
                </div>` : ""}
                <div class="detail-field">
                    <div class="field-label">Subject</div>
                    <div class="field-value">${escapeHtml(n.subject)}</div>
                </div>
                <div class="detail-field">
                    <div class="field-label">Received</div>
                    <div class="field-value">${formatDateTime(n.received_at)}</div>
                </div>
                ${n.actioned_by ? `
                <div class="detail-field">
                    <div class="field-label">Actioned By</div>
                    <div class="field-value">${escapeHtml(n.actioned_by)} at ${formatDateTime(n.actioned_at)}</div>
                </div>` : ""}

                <div class="detail-actions">
                    <button class="btn btn-email" onclick="event.stopPropagation(); openEmailModal(${n.id})">Email Task</button>
                    ${n.status !== "actioned" ? `<button class="btn btn-action" onclick="event.stopPropagation(); updateStatus(${n.id}, 'actioned')">Mark Actioned</button>` : ""}
                    ${n.status !== "reviewed" ? `<button class="btn btn-review" onclick="event.stopPropagation(); updateStatus(${n.id}, 'reviewed')">Mark Reviewed</button>` : ""}
                    ${n.status !== "dismissed" ? `<button class="btn btn-dismiss" onclick="event.stopPropagation(); updateStatus(${n.id}, 'dismissed')">Dismiss</button>` : ""}
                    ${n.status !== "new" ? `<button class="btn btn-reopen" onclick="event.stopPropagation(); updateStatus(${n.id}, 'new')">Reopen</button>` : ""}
                </div>
            </div>
            <div class="detail-section">
                <h4>Notes</h4>
                <div class="note-input-area" onclick="event.stopPropagation()">
                    <input type="text" id="note-input-${n.id}" placeholder="Add a note..."
                        onkeydown="if(event.key==='Enter'){addNote(${n.id})}">
                    <button onclick="addNote(${n.id})">Add</button>
                </div>
                ${n.notes ? `<div class="existing-notes">${escapeHtml(n.notes)}</div>` : ""}

                <h4 style="margin-top:16px">Full Email Content</h4>
                <div class="full-body-text">${formatFullBody(n.full_body)}</div>
            </div>
        </div>`;
}

// --- Interactions ---

function toggleExpand(id, event) {
    if (event.target.type === "checkbox") return;
    state.expandedId = state.expandedId === id ? null : id;
    renderTable();
}

function toggleSelect(id, checked) {
    if (checked) {
        state.selectedIds.add(id);
    } else {
        state.selectedIds.delete(id);
    }
    renderTable();
}

function toggleSelectAll(checked) {
    if (checked) {
        state.notifications.forEach(n => state.selectedIds.add(n.id));
    } else {
        state.selectedIds.clear();
    }
    renderTable();
}

function updateBulkActions() {
    const bar = document.getElementById("bulk-actions");
    const count = state.selectedIds.size;
    if (count > 0) {
        bar.classList.add("visible");
        document.getElementById("selected-count").textContent = `${count} selected`;
    } else {
        bar.classList.remove("visible");
    }
}

function filterByCategory(category) {
    // Toggle filter
    if (state.filters.category === category) {
        delete state.filters.category;
    } else {
        state.filters.category = category;
    }
    // Update active state on cards
    document.querySelectorAll(".stat-card").forEach(c => c.classList.remove("active"));
    if (state.filters.category) {
        const cardMap = {
            action_required: "card-action",
            review: "card-review",
            info: "card-info",
        };
        const card = document.getElementById(cardMap[category]);
        if (card) card.classList.add("active");
    }
    fetchNotifications();
}

function filterByStatus(status) {
    if (state.filters.status === status) {
        delete state.filters.status;
        state.filters.hide_closed = "true";
    } else {
        state.filters.status = status;
        // When explicitly viewing a status, don't hide anything
        delete state.filters.hide_closed;
    }
    document.querySelectorAll(".stat-card").forEach(c => c.classList.remove("active"));
    if (state.filters.status === "new") {
        document.getElementById("card-new").classList.add("active");
    } else if (state.filters.status === "actioned") {
        document.getElementById("card-done").classList.add("active");
    }
    document.getElementById("filter-status").value = state.filters.status || "";
    fetchNotifications();
}

function applyFilters() {
    state.filters.matter = document.getElementById("filter-matter").value || undefined;
    state.filters.search = document.getElementById("filter-search").value || undefined;
    const statusVal = document.getElementById("filter-status").value;
    if (statusVal) {
        state.filters.status = statusVal;
        delete state.filters.hide_closed;
    } else {
        delete state.filters.status;
        state.filters.hide_closed = "true";
    }
    // Clean undefined values
    state.filters = Object.fromEntries(
        Object.entries(state.filters).filter(([_, v]) => v !== undefined && v !== "")
    );
    fetchNotifications();
}

function clearFilters() {
    state.filters = { hide_closed: "true" };
    document.getElementById("filter-matter").value = "";
    document.getElementById("filter-search").value = "";
    document.getElementById("filter-status").value = "";
    document.querySelectorAll(".stat-card").forEach(c => c.classList.remove("active"));
    fetchNotifications();
}

function setUser() {
    const select = document.getElementById("user-select");
    state.currentUser = select.value;
    localStorage.setItem("pexaUser", state.currentUser);
}

// --- Mobile Quick Filters ---

state.quickFilter = null; // 'today', 'tomorrow', 'week', or null

function quickFilter(mode) {
    // Clear active states
    document.querySelectorAll(".quick-filter-btn").forEach(b => b.classList.remove("active"));

    if (mode === "clear" || state.quickFilter === mode) {
        // Toggle off or clear
        state.quickFilter = null;
        state.sortField = "received_at";
        state.sortDir = "desc";
    } else if (mode === "sort") {
        // Just sort by settlement date ascending (soonest first)
        state.quickFilter = null;
        state.sortField = "settlement_date";
        state.sortDir = "asc";
        document.getElementById("qf-sort-settlement").classList.add("active");
    } else {
        state.quickFilter = mode;
        state.sortField = "settlement_date";
        state.sortDir = "asc";
        document.getElementById("qf-" + mode).classList.add("active");
    }
    renderTable();
}

function applyQuickFilter(notifications) {
    if (!state.quickFilter) return notifications;

    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());

    return notifications.filter(n => {
        if (!n.settlement_date) return false;
        const match = n.settlement_date.match(/(\d{1,2})\/(\d{1,2})\/(\d{4})/);
        if (!match) return false;
        const sd = new Date(parseInt(match[3]), parseInt(match[2]) - 1, parseInt(match[1]));
        const diffDays = (sd - today) / (1000 * 60 * 60 * 24);

        if (state.quickFilter === "today") return diffDays >= 0 && diffDays < 1;
        if (state.quickFilter === "tomorrow") return diffDays >= 0 && diffDays < 2;
        if (state.quickFilter === "week") return diffDays >= 0 && diffDays <= 7;
        return true;
    });
}

function toggleFilters() {
    const bar = document.getElementById("filters-bar");
    const icon = document.getElementById("filter-toggle-icon");
    bar.classList.toggle("mobile-open");
    icon.innerHTML = bar.classList.contains("mobile-open") ? "&#9650;" : "&#9660;";
}

// --- Sorting ---

function sortBy(field) {
    if (state.sortField === field) {
        state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
    } else {
        state.sortField = field;
        state.sortDir = "desc";
    }
    renderTable();
    updateSortIndicators();
}

function parseSettlementDate(dateStr) {
    // Parse Australian format: "14/04/2026 02:30 PM AEST" or "13/03/2026 02:00 PM AEDT"
    if (!dateStr) return null;
    const match = dateStr.match(/(\d{1,2})\/(\d{1,2})\/(\d{4})(?:\s+(\d{1,2}):(\d{2})\s*(AM|PM))?/i);
    if (!match) return null;
    const day = parseInt(match[1], 10);
    const month = parseInt(match[2], 10) - 1;
    const year = parseInt(match[3], 10);
    let hours = match[4] ? parseInt(match[4], 10) : 0;
    const minutes = match[5] ? parseInt(match[5], 10) : 0;
    if (match[6] && match[6].toUpperCase() === "PM" && hours !== 12) hours += 12;
    if (match[6] && match[6].toUpperCase() === "AM" && hours === 12) hours = 0;
    return new Date(year, month, day, hours, minutes);
}

function sortNotifications(notifications) {
    return [...notifications].sort((a, b) => {
        let aVal = a[state.sortField] || "";
        let bVal = b[state.sortField] || "";

        // Category priority sorting
        if (state.sortField === "category") {
            const order = { action_required: 0, review: 1, info: 2 };
            aVal = order[aVal] ?? 3;
            bVal = order[bVal] ?? 3;
        }

        // Settlement date: parse as real dates for proper sorting
        if (state.sortField === "settlement_date") {
            const aDate = parseSettlementDate(aVal);
            const bDate = parseSettlementDate(bVal);
            // Push empty/null dates to the end
            if (!aDate && !bDate) return 0;
            if (!aDate) return 1;
            if (!bDate) return -1;
            aVal = aDate.getTime();
            bVal = bDate.getTime();
        }

        if (aVal < bVal) return state.sortDir === "asc" ? -1 : 1;
        if (aVal > bVal) return state.sortDir === "asc" ? 1 : -1;
        return 0;
    });
}

function updateSortIndicators() {
    document.querySelectorAll("thead th[data-sort]").forEach(th => {
        const icon = th.querySelector(".sort-icon");
        if (th.dataset.sort === state.sortField) {
            th.classList.add("sorted");
            icon.textContent = state.sortDir === "asc" ? " ▲" : " ▼";
        } else {
            th.classList.remove("sorted");
            icon.textContent = " ⇅";
        }
    });
}

// --- Helpers ---

function formatFullBody(text) {
    if (!text) return "No content available";

    // Try to find message body in "New Message" type PEXA notifications
    // The message sits between "Subject: ..." and "Note: Sensitive data..."
    const lines = text.split('\n');
    let subjectLineIdx = -1;
    let noteLineIdx = -1;
    let subscriberRefIdx = -1;

    for (let i = 0; i < lines.length; i++) {
        const trimmed = lines[i].trim().toLowerCase();
        if (trimmed.startsWith('subject:') && subjectLineIdx === -1) {
            subjectLineIdx = i;
        }
        if (trimmed.startsWith('note:') && trimmed.includes('sensitive data') && noteLineIdx === -1) {
            noteLineIdx = i;
        }
        if (trimmed.match(/^subscriber\s+ref/) && subscriberRefIdx === -1) {
            subscriberRefIdx = i;
        }
    }

    // Determine message boundaries
    let msgStart = -1;
    let msgEnd = -1;

    if (subjectLineIdx >= 0) {
        msgStart = subjectLineIdx + 1;
        msgEnd = noteLineIdx >= 0 ? noteLineIdx : (subscriberRefIdx >= 0 ? subscriberRefIdx : -1);
    }

    if (msgStart >= 0 && msgEnd > msgStart) {
        // Extract message lines (trim empty lines from start/end)
        let messageLines = lines.slice(msgStart, msgEnd);
        while (messageLines.length > 0 && messageLines[0].trim() === '') messageLines.shift();
        while (messageLines.length > 0 && messageLines[messageLines.length - 1].trim() === '') messageLines.pop();

        if (messageLines.length > 0) {
            const before = lines.slice(0, msgStart).map(l => escapeHtml(l)).join('\n');
            const message = messageLines.map(l => escapeHtml(l)).join('\n');
            const after = lines.slice(msgEnd).map(l => escapeHtml(l)).join('\n');

            return before + '\n<div class="message-body-highlight">' + message + '</div>\n' + after;
        }
    }

    // Fallback: no message body identified, return as plain escaped text
    return escapeHtml(text);
}

function escapeHtml(str) {
    if (!str) return "";
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

function truncate(str, len) {
    if (!str) return "";
    return str.length > len ? escapeHtml(str.slice(0, len)) + "..." : escapeHtml(str);
}

function formatDate(dateStr) {
    if (!dateStr) return "N/A";
    return escapeHtml(dateStr);
}

function formatSettlementDateOnly(dateStr) {
    // Extract just the date portion (e.g. "14/04/2026") from "14/04/2026 02:30 PM AEST"
    if (!dateStr) return "N/A";
    const match = dateStr.match(/(\d{1,2}\/\d{1,2}\/\d{4})/);
    return match ? match[1] : dateStr;
}

function isSettlementTodayOrTomorrow(dateStr) {
    // Check if settlement date is today or tomorrow (Australian dd/mm/yyyy format)
    if (!dateStr) return false;
    const match = dateStr.match(/(\d{1,2})\/(\d{1,2})\/(\d{4})/);
    if (!match) return false;
    const settlement = new Date(parseInt(match[3]), parseInt(match[2]) - 1, parseInt(match[1]));
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const diffDays = (settlement - today) / (1000 * 60 * 60 * 24);
    return diffDays >= 0 && diffDays <= 1;
}

function formatDateTime(isoStr) {
    if (!isoStr) return "N/A";
    try {
        const d = new Date(isoStr);
        if (isNaN(d.getTime())) return escapeHtml(isoStr);
        return d.toLocaleDateString("en-AU", {
            day: "2-digit", month: "2-digit", year: "numeric",
            hour: "2-digit", minute: "2-digit",
        });
    } catch {
        return escapeHtml(isoStr);
    }
}

function isSettlementSoon(dateStr) {
    if (!dateStr) return false;
    // Try to parse Australian date format dd/mm/yyyy
    const match = dateStr.match(/(\d{2})\/(\d{2})\/(\d{4})/);
    if (!match) return false;
    const d = new Date(match[3], match[2] - 1, match[1]);
    const now = new Date();
    const diffDays = (d - now) / (1000 * 60 * 60 * 24);
    return diffDays >= 0 && diffDays <= 3;
}

function showToast(message, type = "success") {
    const container = document.getElementById("toast-container");
    const toast = document.createElement("div");
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => toast.remove(), 4000);
}

// --- Initialization ---

async function refreshAll() {
    await Promise.all([fetchNotifications(), fetchStats()]);
}

document.addEventListener("DOMContentLoaded", async () => {
    // Set saved user
    const userSelect = document.getElementById("user-select");
    userSelect.value = state.currentUser;

    // Check connection
    await checkConnection();

    // Load data
    await refreshAll();

    // Auto-refresh every 60 seconds
    state.autoRefreshInterval = setInterval(refreshAll, 60000);

    // Filter input listeners
    document.getElementById("filter-matter").addEventListener("input", debounce(applyFilters, 500));
    document.getElementById("filter-search").addEventListener("input", debounce(applyFilters, 500));
    document.getElementById("filter-status").addEventListener("change", applyFilters);
});

function debounce(fn, ms) {
    let timer;
    return (...args) => {
        clearTimeout(timer);
        timer = setTimeout(() => fn(...args), ms);
    };
}
