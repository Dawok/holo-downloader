const csrfToken = document.querySelector('meta[name="csrf-token"]').content;

function hideBrokenHistoryThumbnails(root) {
    for (const image of root.querySelectorAll('[data-history-thumbnail]')) {
        if (image.complete && image.naturalWidth === 0) image.hidden = true;
    }
}

document.addEventListener('error', event => {
    if (event.target instanceof HTMLImageElement && event.target.hasAttribute('data-history-thumbnail')) {
        event.target.hidden = true;
    }
}, true);
hideBrokenHistoryThumbnails(document);

function filterItems(scope) {
    const query = (scope.querySelector('[data-filter-search]')?.value || '').trim().toLocaleLowerCase();
    const mode = scope.querySelector('[data-filter-mode]')?.value || 'all';
    const state = scope.querySelector('[data-filter-state]')?.value || 'all';
    const items = Array.from(scope.querySelectorAll('.filter-item'));
    let shown = 0;
    for (const item of items) {
        const matches = (item.dataset.search || '').toLocaleLowerCase().includes(query)
            && (mode === 'all' || (item.dataset.modes || '').split(' ').includes(mode))
            && (state === 'all' || (item.dataset.state || '').includes(state));
        item.hidden = !matches;
        shown += Number(matches);
    }
    const empty = scope.querySelector('.filter-empty');
    if (empty) empty.hidden = shown > 0 || items.length === 0;
    const count = scope.querySelector('[data-filter-count]');
    if (count) count.textContent = `${shown} of ${items.length} channels`;
}

for (const scope of document.querySelectorAll('.filter-scope')) {
    scope.addEventListener('input', () => filterItems(scope));
    scope.addEventListener('change', () => filterItems(scope));
    filterItems(scope);
}

function updateCounters(container) {
    for (const counter of container.querySelectorAll('[data-counter]')) {
        for (const target of document.querySelectorAll(`[data-metric="${counter.dataset.counter}"]`)) {
            target.textContent = counter.dataset.count;
        }
    }
}

function selectRecordingTab(container, selected) {
    const tabs = container.matches('[data-recording-tabs]') ? container : container.querySelector('[data-recording-tabs]');
    if (!tabs) return;
    tabs.dataset.selectedTab = selected;
    for (const button of tabs.querySelectorAll('[data-recording-tab]')) {
        const active = button.dataset.recordingTab === selected;
        button.setAttribute('aria-pressed', String(active));
        const panel = tabs.querySelector(`[data-recording-panel="${button.dataset.recordingTab}"]`);
        if (panel) panel.hidden = !active;
    }
}

let pendingConfirmation;

for (const container of document.querySelectorAll('[data-poll]')) {
    let timer;
    let pending = false;
    const interval = Number(container.dataset.interval || 15000);
    const isBusy = () => container.contains(document.activeElement)
        || (document.getElementById('confirm-dialog').open && pendingConfirmation && container.contains(pendingConfirmation));
    const refresh = async () => {
        if (pending) return;
        clearTimeout(timer);
        if (document.hidden || isBusy()) {
            timer = setTimeout(refresh, interval);
            return;
        }
        pending = true;
        try {
            const response = await fetch(container.dataset.poll, {
                headers: { 'X-Requested-With': 'fetch' },
                signal: AbortSignal.timeout(15000),
            });
            if (!response.ok) throw new Error('Update unavailable');
            const html = await response.text();
            const selectedTab = container.querySelector('[data-recording-tabs]')?.dataset.selectedTab;
            const openDetails = new Set(Array.from(container.querySelectorAll('[data-job]:not([hidden])'), detail => detail.dataset.job));
            if (!isBusy()) {
                container.innerHTML = html;
                hideBrokenHistoryThumbnails(container);
                if (selectedTab) selectRecordingTab(container, selectedTab);
                for (const detail of container.querySelectorAll('[data-job]')) {
                    detail.hidden = !openDetails.has(detail.dataset.job);
                    container.querySelector(`[data-toggle-details="${detail.dataset.job}"]`).setAttribute('aria-expanded', String(!detail.hidden));
                }
            }
            updateCounters(container);
            const scope = container.closest('.filter-scope');
            if (scope) filterItems(scope);
            updateTotals();
            updateElapsed();
        } catch (error) {
        } finally {
            pending = false;
            timer = setTimeout(refresh, interval);
        }
    };
    timer = setTimeout(refresh, interval);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
}

document.addEventListener('click', event => {
    const recordingTab = event.target.closest('[data-recording-tab]');
    if (recordingTab) selectRecordingTab(recordingTab.closest('[data-recording-tabs]'), recordingTab.dataset.recordingTab);
    const toggle = event.target.closest('[data-toggle-details]');
    if (toggle) {
        const detail = document.getElementById(toggle.dataset.toggleDetails);
        detail.hidden = !detail.hidden;
        toggle.setAttribute('aria-expanded', String(!detail.hidden));
    }
    const closer = event.target.closest('[data-close-dialog]');
    if (closer) closer.closest('dialog').close();
    const dismiss = event.target.closest('[data-dismiss]');
    if (dismiss) dismiss.closest('.notice').remove();
});
document.addEventListener('submit', async event => {
    const form = event.target;
    if (form.matches('.theme-form')) {
        event.preventDefault();
        const button = form.querySelector('button');
        button.disabled = true;
        try {
            const response = await fetch(form.action, {
                method: 'POST', body: new FormData(form),
                headers: { 'X-Requested-With': 'fetch' }, signal: AbortSignal.timeout(10000),
            });
            if (!response.ok) throw new Error('Appearance could not be saved. Reload the page and try again.');
            const { theme } = await response.json();
            document.documentElement.dataset.theme = theme;
            document.querySelector('meta[name="color-scheme"]').content = theme;
            for (const icon of button.querySelectorAll('[data-theme-icon]')) icon.hidden = icon.dataset.themeIcon !== (theme === 'light' ? 'moon' : 'sun');
            button.setAttribute('aria-label', `Switch to ${theme === 'dark' ? 'light' : 'dark'} appearance`);
        } catch (error) {
            const notice = document.createElement('p');
            notice.className = 'notice notice-danger';
            notice.setAttribute('role', 'alert');
            notice.textContent = 'Appearance could not be saved. Try again.';
            document.getElementById('main-content').prepend(notice);
        } finally {
            button.disabled = false;
        }
        return;
    }
    if (form.dataset.confirm && form.dataset.confirmed !== 'true') {
        event.preventDefault();
        pendingConfirmation = form;
        document.getElementById('confirm-message').textContent = form.dataset.confirm;
        document.getElementById('confirm-dialog').showModal();
    }
});
document.getElementById('confirm-action').addEventListener('click', () => {
    if (!pendingConfirmation) return;
    const form = pendingConfirmation;
    form.dataset.confirmed = 'true';
    document.getElementById('confirm-dialog').close();
    form.requestSubmit();
});
document.getElementById('confirm-dialog').addEventListener('close', () => { pendingConfirmation = null; });
for (const dialog of document.querySelectorAll('dialog')) {
    dialog.addEventListener('click', event => {
        if (event.target !== dialog) return;
        const bounds = dialog.getBoundingClientRect();
        if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
    });
}

const lookupButton = document.getElementById('lookup-channel');
if (lookupButton) {
    lookupButton.addEventListener('click', async () => {
        const source = document.getElementById('channel-source').value.trim();
        const status = document.getElementById('lookup-status');
        status.className = 'field-help';
        if (!source) {
            status.textContent = 'Enter a YouTube channel link, @handle, or ID first.';
            status.classList.add('is-error');
            document.getElementById('channel-source').focus();
            return;
        }
        lookupButton.disabled = true;
        lookupButton.querySelector('span').textContent = 'Looking up…';
        status.textContent = 'Finding channel details on YouTube…';
        try {
            const response = await fetch('/api/channels/resolve', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
                body: JSON.stringify({ source }),
                signal: AbortSignal.timeout(30000),
            });
            const result = await response.json();
            if (!response.ok) throw new Error(result.error || 'Could not look up this channel.');
            document.getElementById('channel-name').value = result.name;
            document.getElementById('channel-id').value = result.id;
            if (result.edit_url) {
                status.textContent = 'This channel is already in your collection. ';
                const link = document.createElement('a');
                link.href = result.edit_url;
                link.textContent = 'Edit its settings';
                link.className = 'text-link';
                status.append(link);
            } else {
                status.textContent = `Found ${result.name}. Choose your archive options below.`;
                status.classList.add('is-success');
            }
        } catch (error) {
            status.textContent = error.name === 'TimeoutError'
                ? 'Lookup took too long. Try again, or enter the name and channel ID manually.'
                : error.message;
            status.classList.add('is-error');
        } finally {
            lookupButton.disabled = false;
            lookupButton.querySelector('span').textContent = 'Look up';
        }
    });
    document.getElementById('channel-source').addEventListener('keydown', event => {
        if (event.key === 'Enter') {
            event.preventDefault();
            lookupButton.click();
        }
    });
}

const copySettings = document.getElementById('copy-settings');
if (copySettings) {
    const channels = JSON.parse(document.getElementById('channel-settings').textContent);
    copySettings.addEventListener('change', () => {
        const channel = channels.find(item => item.id === copySettings.value);
        if (!channel) return;
        for (const mode of ['public', 'unarchived', 'members', 'community']) document.getElementById(`mode-${mode}`).checked = channel[mode];
        document.getElementById('title-regex').value = channel.title_regex;
        document.getElementById('description-regex').value = channel.description_regex;
        const status = document.getElementById('copy-status');
        status.textContent = `Copied archive options and filters from ${channel.name}. Save to apply.`;
        status.classList.add('is-success');
    });
}


function updateTotals() {
    const total = Array.from(document.querySelectorAll('[data-size]')).reduce((sum, row) => sum + Number(row.dataset.size || 0), 0);
    let value = total;
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
    for (const target of document.querySelectorAll('[data-total-size]')) target.textContent = `${value === 0 ? '0' : value.toFixed(2)} ${units[unit]}`;
}

function updateElapsed() {
    for (const target of document.querySelectorAll('[data-started]')) {
        const seconds = Math.max(0, Math.floor(Date.now() / 1000 - Number(target.dataset.started)));
        target.textContent = [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60].map(value => String(value).padStart(2, '0')).join(':');
    }
}

updateTotals();
updateElapsed();
if (document.querySelector('[data-started]') || document.querySelector('[data-total-size]')) setInterval(updateElapsed, 1000);

document.addEventListener('error', event => {
    if (event.target instanceof HTMLImageElement) event.target.hidden = true;
}, true);
for (const img of document.querySelectorAll('img')) if (img.complete && !img.naturalWidth) img.hidden = true;
