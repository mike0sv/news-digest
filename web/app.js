'use strict';

const $ = (id) => document.getElementById(id);
const names = { links: 'Без AI', gemini: 'Gemini', anthropic: 'Claude API', openai: 'OpenAI', 'claude-cli': 'Claude Code', codex: 'Codex' };
const symbols = ['✳', '⌘', '◈', '○'];
let archive;
let state = { view: 'latest', issue: '', group: '', provider: '', query: '' };
let saved = loadLocal('utro:saved');
let read = loadLocal('utro:read');
let toastTimer;

function loadLocal(key) {
  try { return new Set(JSON.parse(localStorage.getItem(key) || '[]')); }
  catch { return new Set(); }
}
function persist(key, value) {
  try { localStorage.setItem(key, JSON.stringify([...value])); }
  catch { toast('Браузер не разрешил сохранить отметку.'); }
}
function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
function date(value, options = {}) {
  return new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long', ...options })
    .format(new Date(value.length === 10 ? value + 'T12:00:00Z' : value));
}
function plural(number, forms) {
  const digit = number % 10, tens = number % 100;
  return forms[(digit === 1 && tens !== 11) ? 0 : (digit >= 2 && digit <= 4 && (tens < 12 || tens > 14)) ? 1 : 2];
}
function safeLink(url) {
  try { const parsed = new URL(url, location.href); return ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : '#'; }
  catch { return '#'; }
}
function toast(message) {
  $('toast').textContent = message;
  $('toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, 2800);
}
function icon(kind) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', kind === 'save' ? 'M6 3h12v18l-6-4-6 4z' : 'm5 12 4 4L19 6');
  if (kind === 'save') path.classList.add('bookmark');
  svg.append(path);
  return svg;
}
function navigate(patch) {
  state = { ...state, ...patch };
  updateUrl();
  render();
}
function updateUrl() {
  const params = new URLSearchParams();
  if (state.view !== 'latest') params.set('view', state.view);
  if (state.issue) params.set('issue', state.issue);
  if (state.group) params.set('group', state.group);
  if (state.provider) params.set('provider', state.provider);
  if (state.query) params.set('q', state.query);
  history.replaceState(null, '', location.pathname + (params.size ? '?' + params : ''));
}
function fromUrl() {
  const params = new URLSearchParams(location.search);
  state = { view: params.get('view') || (params.has('issue') ? 'issue' : 'latest'),
    issue: params.get('issue') || '', group: params.get('group') || '',
    provider: params.get('provider') || '', query: params.get('q') || '' };
  if (!['latest', 'all', 'saved', 'issue'].includes(state.view)) state.view = 'latest';
  if (state.issue && !archive.issues.some((issue) => issue.id === state.issue)) {
    state.view = 'latest'; state.issue = ''; toast('Выпуск не найден. Открыт последний.');
  }
  if (state.group && !allGroups().some((group) => group.id === state.group)) state.group = '';
}
function allGroups() {
  const result = new Map(archive.groups.map((group) => [group.id, group]));
  for (const issue of archive.issues) for (const group of issue.groups) if (!result.has(group.id)) result.set(group.id, group);
  return [...result.values()];
}
function selectedIssue() {
  if (state.view === 'latest') return archive.issues[0];
  if (state.view === 'issue') return archive.issues.find((issue) => issue.id === state.issue);
  return null;
}
function rowsForView({ ignoreGroup = false } = {}) {
  // Search always includes the whole archive. Preserve the chosen view when cleared.
  const issue = selectedIssue();
  const issues = state.query ? archive.issues : issue ? [issue] : archive.issues;
  const query = state.query.trim().toLocaleLowerCase('ru');
  return issues.flatMap((entry) => entry.stories.map((story) => ({ issue: entry, story })))
    .filter(({ issue: entry, story }) => (ignoreGroup || !state.group || story.group === state.group)
      && (!state.provider || entry.provider === state.provider)
      && (state.view !== 'saved' || saved.has(story.id))
      && (!query || [story.title, story.summary, story.why_it_matters, ...story.sources.map((source) => source.source)]
        .join(' ').toLocaleLowerCase('ru').includes(query)));
}
function navButton(label, count, active, handler) {
  const button = node('button', 'nav-button' + (active ? ' active' : ''));
  button.type = 'button';
  button.append(node('span', '', label), node('span', '', String(count)));
  button.setAttribute('aria-pressed', String(active));
  button.addEventListener('click', handler);
  return button;
}
function renderNav() {
  $('latest-count').textContent = archive.issues[0]?.stories.length ?? 0;
  $('archive-count').textContent = archive.issues.length;
  const known = new Set(archive.issues.flatMap((issue) => issue.stories.map((story) => story.id)));
  $('saved-count').textContent = [...saved].filter((id) => known.has(id)).length;
  for (const view of ['latest', 'all', 'saved']) {
    $(view).classList.toggle('active', state.view === view && !state.query);
    $(view).setAttribute('aria-pressed', String(state.view === view && !state.query));
  }
  const rows = rowsForView({ ignoreGroup: true });
  $('group-nav').replaceChildren();
  allGroups().forEach((group, index) => {
    const button = navButton(group.name, rows.filter(({ story }) => story.group === group.id).length,
      state.group === group.id, () => navigate({ group: state.group === group.id ? '' : group.id }));
    const label = button.firstElementChild;
    label.className = 'topic-label';
    label.prepend(node('span', 'topic-symbol', symbols[index % symbols.length]));
    $('group-nav').append(button);
  });
  $('issue-nav').replaceChildren();
  for (const issue of archive.issues.slice(0, 14)) {
    const button = navButton(date(issue.date), issue.stories.length,
      state.view === 'issue' && state.issue === issue.id && !state.query,
      () => { $('search').value = ''; navigate({ view: 'issue', issue: issue.id, query: '' }); });
    button.classList.add('issue-button');
    button.title = `${issue.id} · ${names[issue.provider] || issue.provider}`;
    $('issue-nav').append(button);
  }
}
function card({ issue, story }) {
  const article = node('article', 'story-card' + (read.has(story.id) ? ' read' : ''));
  const top = node('div', 'card-top');
  const kicker = node('div', 'card-kicker');
  kicker.append(node('span', '', date(issue.date)), node('span', 'provider-badge', names[issue.provider] || issue.provider));
  if (story.is_update) kicker.append(node('span', 'update-badge', 'Развитие истории'));
  const buttons = node('div', 'card-buttons');
  for (const kind of ['read', 'save']) {
    const button = node('button', 'icon-button');
    button.type = 'button';
    const set = kind === 'save' ? saved : read;
    const label = kind === 'save' ? 'Сохранить в этом браузере' : 'Отметить прочитанным';
    button.title = label;
    button.setAttribute('aria-label', `${label}: ${story.title}`);
    button.setAttribute('aria-pressed', String(set.has(story.id)));
    button.append(icon(kind));
    button.addEventListener('click', () => {
      if (set.has(story.id)) set.delete(story.id); else set.add(story.id);
      persist(kind === 'save' ? 'utro:saved' : 'utro:read', set);
      render();
      if (kind === 'save' && set.has(story.id)) toast('Сохранено в этом браузере.');
    });
    buttons.append(button);
  }
  top.append(kicker, buttons);
  const heading = node('h3');
  const title = node('a', '', story.title);
  title.href = safeLink(story.sources[0]?.url);
  title.target = '_blank'; title.rel = 'noopener noreferrer';
  heading.append(title);
  article.append(top, heading, node('p', 'summary', story.summary));
  if (story.why_it_matters) article.append(node('p', 'relevance', story.why_it_matters));
  const footer = node('div', 'card-footer');
  const links = node('div', 'source-links');
  for (const source of story.sources) {
    const link = node('a', 'source-link', source.source);
    link.href = safeLink(source.url); link.target = '_blank'; link.rel = 'noopener noreferrer';
    link.title = source.title; links.append(link);
  }
  footer.append(links);
  if (issue.provider === 'links') footer.append(node('span', 'excerpt-note', 'Выдержка из ленты'));
  article.append(footer);
  return article;
}
function render() {
  const issue = selectedIssue();
  const searching = Boolean(state.query);
  $('search').value = state.query;
  $('provider').value = state.provider;
  $('group-filter').value = state.group;
  $('page-title').textContent = searching ? 'Найдено в архиве.' : state.view === 'saved' ? 'На потом.'
    : state.view === 'all' ? 'Все ваши утра.' : 'Хорошего утра.';
  $('edition-label').textContent = searching ? 'ПОИСК ПО ВСЕМ ВЫПУСКАМ'
    : state.view === 'saved' ? 'СОХРАНЕНО В ЭТОМ БРАУЗЕРЕ'
    : state.view === 'all' ? 'ИСТОРИЯ ВАШИХ ДАЙДЖЕСТОВ' : 'ВАШ УТРЕННИЙ ОБЗОР';
  $('intro').textContent = searching ? `Результаты по запросу «${state.query}».`
    : state.view === 'saved' ? 'Публикации, к которым хочется вернуться. Отметки хранятся только в этом браузере.'
    : state.view === 'all' ? 'Все выпуски в одном месте. Выберите тему или найдите нужную публикацию.'
    : issue?.intro || 'Первый выпуск появится после запуска сбора новостей.';
  $('today-label').textContent = issue && !searching ? date(issue.date, { weekday: 'long', year: 'numeric' }) : 'Личный новостной обзор';
  $('download').hidden = !issue || searching;
  if (issue) $('download').href = `issues/${encodeURIComponent(issue.id)}.md`;
  $('issue-meta').replaceChildren();
  if (issue && !searching) {
    const stats = issue.stats;
    for (const text of [`${stats.selected} ${plural(stats.selected, ['сюжет', 'сюжета', 'сюжетов'])}`,
      `${stats.sources_ok} из ${stats.sources_total} источников`,
      `${Math.max(1, Math.ceil(issue.stories.reduce((sum, story) => sum + story.summary.length, 0) / 1200))} мин чтения`]) {
      $('issue-meta').append(node('span', '', text));
    }
    const label = issue.provider === 'links' ? 'Без AI · без проверки релевантности' : `${names[issue.provider] || issue.provider}${issue.model ? ' · ' + issue.model : ''}`;
    $('issue-meta').append(node('span', 'meta-pill', label));
  }
  const rows = rowsForView();
  $('stories').replaceChildren();
  for (const group of allGroups()) {
    const grouped = rows.filter(({ story }) => story.group === group.id);
    if (!grouped.length) continue;
    const section = node('section', 'story-group');
    const heading = node('div', 'group-heading');
    heading.append(node('h2', '', group.name), node('span', 'group-line'), node('span', 'group-count', String(grouped.length)));
    section.append(heading, ...grouped.map(card));
    $('stories').append(section);
  }
  $('stories').setAttribute('aria-busy', 'false');
  $('empty').hidden = rows.length > 0;
  $('empty').querySelector('h2').textContent = !archive.issues.length ? 'Первое утро ещё впереди' : 'Здесь пока тихо';
  $('empty').querySelector('p').textContent = !archive.issues.length
    ? 'Запустите сбор в GitHub Actions. Здесь появятся выпуски и ссылки на оригиналы.'
    : state.view === 'saved' && !state.query ? 'Сохраните публикацию с помощью закладки на карточке.'
    : 'Новых публикаций нет или выбранные фильтры не дали результатов.';
  $('result-count').textContent = `${rows.length} ${plural(rows.length, ['публикация', 'публикации', 'публикаций'])}`;
  $('reset').hidden = !state.query && !state.group && !state.provider;
  renderNav();
}
function renderSources() {
  const panel = $('sources-panel');
  panel.append(node('h2', '', 'Откуда приходят новости'), node('p', '',
    'Источники вашего дайджеста. Темы и подписки меняются в config/settings.json в репозитории.'));
  const health = new Map((archive.status.source_health || []).map((source) => [source.id, source]));
  for (const source of archive.sources) {
    const row = node('div', 'source-row'), link = node('a', '', source.name);
    link.href = safeLink(source.url); link.target = '_blank'; link.rel = 'noopener noreferrer';
    const check = health.get(source.id);
    row.append(link, node('span', '', !check ? 'Ещё не проверен' : check.ok ? `${check.count} в ленте` : 'Сбор не удался'));
    panel.append(row);
  }
}
async function start() {
  try {
    const response = await fetch('archive.json', { cache: 'no-cache' });
    if (!response.ok) throw new Error('Archive unavailable');
    archive = await response.json();
    if (!Array.isArray(archive.issues)) throw new Error('Invalid archive');
    document.title = `${archive.title} — новостной обзор`;
    $('schedule').textContent = `После ${String(archive.morning_hour).padStart(2, '0')}:17 · ${archive.timezone}`;
    $('source-count').textContent = archive.sources.length;
    $('last-updated').textContent = archive.status.last_success ? `Обновлено ${date(archive.status.last_success)}` : 'Ожидает первого выпуска';
    for (const provider of new Set(archive.issues.map((issue) => issue.provider))) {
      const option = node('option', '', names[provider] || provider); option.value = provider; $('provider').append(option);
    }
    for (const group of allGroups()) {
      const option = node('option', '', group.name); option.value = group.id; $('group-filter').append(option);
    }
    renderSources(); fromUrl(); render();
    for (const view of ['latest', 'all', 'saved']) $(view).addEventListener('click', () => navigate({ view, issue: '', query: '' }));
    let timer;
    $('search').addEventListener('input', (event) => { clearTimeout(timer); const query = event.target.value; timer = setTimeout(() => navigate({ query }), 150); });
    $('provider').addEventListener('change', (event) => navigate({ provider: event.target.value }));
    $('group-filter').addEventListener('change', (event) => navigate({ group: event.target.value }));
    $('reset').addEventListener('click', () => navigate({ group: '', query: '', provider: '' }));
    $('sources-toggle').addEventListener('click', () => { const hidden = !$('sources-panel').hidden; $('sources-panel').hidden = hidden; $('sources-toggle').setAttribute('aria-expanded', String(!hidden)); });
    window.addEventListener('popstate', () => { fromUrl(); render(); });
  } catch {
    $('intro').textContent = 'Архив не удалось загрузить. Попробуйте обновить страницу.';
    $('stories').setAttribute('aria-busy', 'false');
    $('result-count').textContent = 'Ошибка загрузки';
    $('download').hidden = true;
  }
}
start();
