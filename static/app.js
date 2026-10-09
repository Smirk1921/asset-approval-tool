/* ============================================================
   图包素材审批工具 · 前端逻辑
   纯原生 JS，无外部依赖，可离线运行
   ============================================================ */
'use strict';

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

/* ------------------------------------------------------------ 工具 */

function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

async function apiGet(url) {
  const res = await fetch(url, { headers: { 'Accept': 'application/json' } });
  const data = await res.json().catch(() => ({ ok: false, error: '返回内容无法解析' }));
  if (!res.ok || data.ok === false) throw new Error(data.error || ('请求失败 ' + res.status));
  return data;
}

async function apiPost(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({ ok: false, error: '返回内容无法解析' }));
  if (!res.ok || data.ok === false) throw new Error(data.error || ('请求失败 ' + res.status));
  return data;
}

/* ------------------------------------------------------------ 全局状态 */

/** 全局应用状态。显式挂到 window 上，让 model.js 这种独立脚本也能读到。 */
const S = window.S = {
  cfg: null,
  set: null,               // 当前素材集 spec
  items: [],               // 精简清单（服务端顺序）
  itemByKey: new Map(),    // key -> item，给 URL 拼版本号用
  statuses: {},            // key -> {s,t,n,c,x}
  filtered: [],            // 通过筛选的下标（指向 items）
  cursor: 0,               // filtered 中的位置
  view: 'detail',
  wallPage: 1,
  wallSide: 'b',
  wallCell: 168,
  wallDim: true,
  selectMode: false,
  selected: new Set(),

  filters: { status: 'all', tags: new Set(), facets: {}, search: '', special: new Set(), prevIds: null },
  stats: {},
  staleDismissed: false,

  autoAdvance: true,

  cur: null,               // 当前素材（精简项）
  draft: null,             // 当前编辑中的审批内容
  itemCache: new Map(),
  annotFocus: -1,
  annotAutoOpened: false,   // 这次圈选是我自动展开的侧栏，输入完要自动收起

  zoom: { scale: 1, tx: 0, ty: 0, sync: true, fit: 1, nat: null, userAdjusted: false },
  overlay: { on: false, mode: 'slider', divider: 50, alpha: 0.5 },

  textFont: 15,
  textWrap: true,
  textSync: true,
  textStacked: false,

  backToWallAfterAnnotate: false,

  theme: 'system',          // system | light | dark

  noteTimer: null,
  toastTimer: null,
  toastAction: null,
  loading: 0,
};

const STATUS_CN = { pass: '通过', reject: '打回', flag: '存疑', '': '未审' };

/* ------------------------------------------------------------ 主题（跟随系统 / 手动切换） */

const THEME_KEY = 'imgreview:theme';

function getThemePref() {
  return localStorage.getItem(THEME_KEY) || 'system';
}

function applyTheme(pref) {
  const dark = pref === 'dark' ||
    (pref === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  document.documentElement.style.colorScheme = dark ? 'dark' : 'light';
  S.theme = pref;
  // 切换主题后重新渲染依赖主题的 UI（比如圈选框颜色、状态点）
  if (S.set) {
    drawAnnotations();
    renderWall();
  }
}

function setTheme(pref) {
  localStorage.setItem(THEME_KEY, pref);
  applyTheme(pref);
  updateThemeButton();
}

function updateThemeButton() {
  const btn = $('#btnTheme');
  if (!btn) return;
  const pref = getThemePref();
  const icons = {
    system: Icons.get('monitor', 15),
    light: Icons.get('sun', 15),
    dark: Icons.get('moon', 15),
  };
  const labels = { system: '跟随系统', light: '浅色', dark: '深色' };
  btn.innerHTML = icons[pref];
  btn.title = '主题：' + labels[pref] + '（点击切换）';
}

function cycleTheme() {
  const order = ['system', 'light', 'dark'];
  const current = order.indexOf(getThemePref());
  setTheme(order[(current + 1) % order.length]);
}

// 跟随系统切换（仅当用户选 system 时）
if (window.matchMedia) {
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (getThemePref() === 'system') applyTheme('system');
  });
}

/* ------------------------------------------------------------ 加载遮罩 */

function busy(on) {
  S.loading += on ? 1 : -1;
  if (S.loading < 0) S.loading = 0;
  $('#loading').style.display = S.loading > 0 ? 'flex' : 'none';
}

/* ------------------------------------------------------------ 提示条 */

function toast(message, options) {
  const opts = options || {};
  const el = $('#toast');
  el.querySelector('.msg').textContent = message;
  el.classList.toggle('err', !!opts.error);
  const btn = $('#toastAction');
  if (opts.actionLabel && typeof opts.action === 'function') {
    btn.style.display = '';
    btn.textContent = opts.actionLabel;
    S.toastAction = opts.action;
  } else {
    btn.style.display = 'none';
    S.toastAction = null;
  }
  el.classList.add('show');
  clearTimeout(S.toastTimer);
  S.toastTimer = setTimeout(() => el.classList.remove('show'), opts.sticky ? 120000 : (opts.duration || 2600));
}

$('#toastAction').addEventListener('click', () => {
  const fn = S.toastAction;
  S.toastAction = null;
  $('#toast').classList.remove('show');
  if (fn) fn();
});

/* ============================================================
   启动
   ============================================================ */

/** 素材集下拉的内容（派生／删除集合后要重建）。 */
function fillSetSelect() {
  $('#setSelect').innerHTML = S.cfg.sets.map((s) => {
    const bad = s.error ? '（配置有误）' : '';
    const count = s.count ? ' · ' + s.count : '';
    return '<option value="' + esc(s.id) + '">' + esc(s.name + bad + count) + '</option>';
  }).join('');
}

async function init() {
  try {
    S.cfg = await apiGet('/api/config');
  } catch (err) {
    document.body.innerHTML = '<div class="fatal"><h2>无法加载配置</h2><pre>' + esc(err.message) + '</pre></div>';
    return;
  }
  S.autoAdvance = !!S.cfg.autoAdvance;

  fillSetSelect();

  $('#cfgPath').textContent = S.cfg.configPath;
  $('#cfgData').textContent = S.cfg.dataDir;
  buildHelp();

  const conflicts = shortcutConflicts();
  if (conflicts.length) {
    toast('快捷键有冲突：' + conflicts.join('；') + '。请改 config.json 里的 tag_shortcuts 或 shortcuts。',
      { error: true, duration: 10000 });
  }

  applyTheme(getThemePref());
  updateThemeButton();

  bindUI();

  const wanted = setFromUrl();
  const preferred = (wanted && S.cfg.sets.find((s) => s.id === wanted))
    || S.cfg.sets.find((s) => !s.error) || S.cfg.sets[0];
  await loadSet(preferred.id);
  startConfigWatch();
}

/** 每隔几秒问一次配置指纹，变了就提示刷新（改了 config.json 不用再自己想起来强刷）。 */
function startConfigWatch() {
  const tick = async () => {
    try {
      const info = await apiGet('/api/ping');
      if (!S.configHash) { S.configHash = info.configHash; return; }
      if (info.configHash && info.configHash !== S.configHash) {
        $('#configWhat').textContent = '素材集或标签可能变了';
        $('#configBar').classList.add('show');
      }
    } catch (err) { /* 服务重启中，忽略这次 */ }
  };
  tick();
  setInterval(tick, 6000);
}

/** 检查标签快捷键有没有和内置快捷键撞车（撞了的话内置的会赢，标签键就哑了） */
function shortcutConflicts() {
  const used = {};
  Object.entries(S.cfg.shortcuts || {}).forEach(([action, keys]) => {
    (keys || []).forEach((key) => { used[String(key).toLowerCase()] = action; });
  });
  const actionCn = {
    next: '下一个', prev: '上一个', pass: '通过', reject: '打回', flag: '存疑', clear: '清除',
    wall: '缩略图墙', detail: '单张对照', annotate: '圈图', overlay: '叠加对比',
    fit: '适应窗口', zoom100: '原始大小', sync: '同步', focusSearch: '搜索',
  };
  const out = [];
  (S.cfg.tagShortcuts || []).forEach((key, index) => {
    const lower = String(key).toLowerCase();
    if (used[lower] && index < S.cfg.tags.length) {
      out.push('「' + S.cfg.tags[index] + '」的 ' + key + ' 被「' + (actionCn[used[lower]] || used[lower]) + '」占用了');
    }
  });
  return out;
}

function buildHelp() {
  const sc = S.cfg.shortcuts || {};
  const keyOf = (action) => (sc[action] || []).map((k) => '<span class="key-hint">' + esc(k) + '</span>').join(' ');
  const tagKeys = S.cfg.tagShortcuts || [];
  const tagList = S.cfg.tags.slice(0, tagKeys.length)
    .map((t, i) => '<span class="key-hint">' + esc(tagKeys[i]) + '</span> ' + esc(t)).join('　');
  $('#helpBody').innerHTML =
    '<div class="hint" style="font-size:12px;line-height:2">' +
    '<b>标记</b>：' + keyOf('pass') + ' 通过　' + keyOf('reject') + ' 打回　' + keyOf('flag') + ' 存疑　' + keyOf('clear') + ' 清除标记<br>' +
    '<b>标签</b>（按一下 = 打回 + 贴标签，一张卡可以连按多个）：<br>' + tagList + '<br>' +
    '<b>翻页</b>：' + keyOf('prev') + ' 上一个　' + keyOf('next') + ' 下一个<br>' +
    '<b>视图</b>：' + keyOf('detail') + ' 单张对照　' + keyOf('wall') + ' 缩略图墙　' + keyOf('overlay') + ' 叠加对比<br>' +
    '<b>图片</b>：滚轮缩放、按住拖拽平移、双击切换适应/原始大小；' +
    keyOf('annotate') + ' 圈图（在图上拖拽画框，画完在右侧写说明）；' +
    keyOf('fit') + ' 适应窗口　' + keyOf('zoom100') + ' 原始大小　' + keyOf('sync') + ' 左右同步<br>' +
    '<b>叠加对比</b>：把原图和成品叠在同一张上，拖中间竖线分界，按住<b>空格</b>临时看全幅原图，' +
    '点「半透明重叠」让成品半透明盖在原图上<br>' +
    '<b>其它</b>：' + keyOf('focusSearch') + ' 跳到搜索框　Ctrl+Z 撤销上一步　Esc 退出圈图/关闭弹窗<br>' +
    '<br><b>圈图说明</b>：圈选框按图片比例保存，缩放平移不影响；导出时坐标是相对图片的百分比，' +
    '「CSV（圈注明细）」还会同时给出像素坐标。<br>' +
    '<b>实时保存</b>：每次点标记都会立刻写盘，中途关掉浏览器不会丢。<br>' +
    '<b>批量操作</b>：作用于「当前筛选结果」（按钮上写着条数）；在缩略图墙里开多选后作用于选中的那些。<br>' +
    '<b>源文件变了怎么办</b>：审批结果会记下当时的源文件指纹。如果你重渲了某张图，工具会提示这些结果可能过期，' +
    '可以一键清掉重新审。<br>' +
    '<b>轮次</b>：审完一批可以「归档本轮，开始新一轮」，结果存成单独文件，当前批次清空重审；' +
    '下一轮可以「筛出上轮打回的」优先复审。<br>' +
    '</div>';
}

/* ============================================================
   素材集加载
   ============================================================ */

/** 当前 URL 上带 ?set= 就打开那个素材集（刷新、分享链接都能停在同一个集合）。 */
function setFromUrl() {
  try {
    return new URLSearchParams(location.search).get('set');
  } catch (err) {
    return null;
  }
}

function rememberSetInUrl(setId) {
  try {
    const url = new URL(location.href);
    url.searchParams.set('set', setId);
    history.replaceState(null, '', url);
  } catch (err) { /* 忽略：不影响使用 */ }
}

async function loadSet(setId) {
  busy(true);
  try {
    const data = await apiGet('/api/set/' + encodeURIComponent(setId) + '/items');
    S.set = data.spec;
    S.items = data.items;
    S.itemByKey = new Map(S.items.map((it) => [it.key, it]));
    S.statuses = data.statuses || {};
    S.stats = data.stats || {};
    S.staleDismissed = false;
    S.itemCache.clear();
    S.selected.clear();
    S.cur = null;
    S.wallPage = 1;
    S.cursor = 0;
    if (S.overlay.on) setOverlayMode(false);
    $('#resultPathHint').textContent = '结果文件：' + (data.resultPath || '');
    $('#setSelect').value = setId;
    rememberSetInUrl(setId);
    configureForType();
    applyFilters();
    clearDetailPane();
    renderAll();

    if (S.set.error) toast('该素材集配置有误：' + S.set.error, { error: true, duration: 6000 });
    if (S.set.warnings && S.set.warnings.length) toast(S.set.warnings.join('；'), { duration: 5000 });
  } catch (err) {
    toast('加载素材集失败：' + err.message, { error: true, duration: 8000 });
  } finally {
    busy(false);
  }
}

/** 标签小胶囊。前几个标签带快捷键提示，让「打回 + 贴标签」能全键盘完成。 */
function tagChipHtml(tag, index, attr) {
  const keys = (S.cfg && S.cfg.tagShortcuts) || [];
  const hint = index < keys.length ? '<em class="key-hint">' + esc(keys[index]) + '</em>' : '';
  return '<span class="tag" ' + attr + '="' + esc(tag) + '" title="' +
    (index < keys.length ? '快捷键 ' + esc(keys[index]) : '') + '">' + esc(tag) + hint + '</span>';
}

/** 按语义把标签分组，组间插分隔符（评审共识：11 个 chip 平铺无层级）。
   分组规则用语义关键词匹配，匹配不到的进「其它」组；顺序保持 config 原序。 */
function tagChipsGrouped() {
  const tags = S.cfg.tags || [];
  const groups = [
    { name: '文字', match: (t) => /标题|字体|黑底|分段|文字|术语/.test(t) },
    { name: '图标', match: (t) => /图标|背景/.test(t) },
    { name: '其它', match: () => true },
  ];
  const assigned = new Set();
  const parts = [];
  groups.forEach((g, gi) => {
    const members = [];
    tags.forEach((t, i) => {
      if (assigned.has(t)) return;
      if (g.match(t)) { members.push(tagChipHtml(t, i, 'data-tag')); assigned.add(t); }
    });
    if (!members.length) return;
    if (parts.length) parts.push('<span class="tag-sep" aria-hidden="true"></span>');
    parts.push('<span class="tag-group" data-group="' + g.name + '">' +
      '<span class="tag-group-name">' + g.name + '</span>' + members.join('') + '</span>');
  });
  return parts.join('');
}

function configureForType() {
  const type = S.set.type;                    // image | text | model
  const isImage = type === 'image';
  const isText = type === 'text';
  const isModel = type === 'model';
  $('#imageCtl').style.display = isImage ? 'flex' : 'none';
  $('#textCtl').style.display = isText ? 'flex' : 'none';
  $('#modelCtl').style.display = isModel ? 'flex' : 'none';
  $('#panes').style.display = isImage ? 'flex' : 'none';
  $('#textWrap').style.display = isText ? 'flex' : 'none';
  $('#modelWrap').style.display = isModel ? 'flex' : 'none';
  $('#roleA').textContent = S.set.sourceA;
  $('#roleB').textContent = S.set.sourceB;
  $('#textRoleA').textContent = S.set.sourceA;
  $('#textRoleB').textContent = S.set.sourceB;
  $('#wallShowA').style.display = isImage ? '' : 'none';
  $('#wallShowB').style.display = isImage ? '' : 'none';
  $('#ovLabelL').textContent = S.set.sourceA;
  $('#ovLabelR').textContent = S.set.sourceB;
  S.wallSide = 'b';
  updateWallSideButtons();

  // 标签（评审共识：11 个 chip 平铺无分组 → 按语义分组，组间加分隔）
  $('#tagChips').innerHTML = tagChipsGrouped();
  $('#batchTags').innerHTML = S.cfg.tags.map((t, i) => tagChipHtml(t, i, 'data-btag')).join('');
  $('#fTags').innerHTML = S.cfg.tags.map((t, i) => tagChipHtml(t, i, 'data-ftag')).join('');
  // 筛选面板里的分面
  renderFacetInputs();
}

/* ============================================================
   筛选
   ============================================================ */

function applyFilters() {
  const f = S.filters;
  const q = (f.search || '').trim().toLowerCase();
  const facetKeys = Object.keys(f.facets);
  const sp = f.special || new Set();
  const out = [];
  for (let i = 0; i < S.items.length; i++) {
    const it = S.items[i];
    const rec = S.statuses[it.key] || {};
    if (f.status !== 'all' && (rec.s || '') !== f.status) continue;
    if (f.tags.size) {
      const tags = rec.t || [];
      let miss = false;
      for (const t of f.tags) { if (tags.indexOf(t) < 0) { miss = true; break; } }
      if (miss) continue;
    }
    if (sp.has('noTags') && (rec.t || []).length) continue;
    if (sp.has('hasAnnot') && !(rec.c || 0)) continue;
    if (sp.has('hasNote') && !(rec.n || 0)) continue;
    if (sp.has('stale') && !rec.x) continue;
    if (sp.has('noTagsReject') && !((rec.s || '') === 'reject' && !(rec.t || []).length)) continue;
    if (f.prevIds && !f.prevIds.has(it.id)) continue;
    let skip = false;
    for (let k = 0; k < facetKeys.length; k++) {
      const field = facetKeys[k];
      const want = f.facets[field];
      if (!want) continue;
      const got = field === '分组' ? it.group : (it.meta || {})[field];
      if (String(got == null ? '' : got) !== want) { skip = true; break; }
    }
    if (skip) continue;
    if (q) {
      const metaValues = it.meta ? Object.values(it.meta).join(' ') : '';
      const hay = (it.id + ' ' + it.name + ' ' + it.group + ' ' + metaValues).toLowerCase();
      if (hay.indexOf(q) < 0) continue;
    }
    out.push(i);
  }
  S.filtered = out;
  if (S.cursor >= out.length) S.cursor = Math.max(0, out.length - 1);
  S.wallPage = 1;
  S.selected.clear();
  updateFilterBadge();
}

function activeFilterCount() {
  const f = S.filters;
  let n = 0;
  if (f.status !== 'all') n++;
  n += f.tags.size;
  n += Object.values(f.facets).filter(Boolean).length;
  n += (f.special ? f.special.size : 0);
  if (f.prevIds) n++;
  if ((f.search || '').trim()) n++;
  return n;
}

const SPECIAL_CN = {
  noTags: '未打任何标签', hasAnnot: '有圈选', hasNote: '有备注',
  stale: '源文件已变', noTagsReject: '打回且无标签',
};

function updateFilterBadge() {
  const n = activeFilterCount();
  const badge = $('#filterCount');
  badge.textContent = n ? String(n) : '';
  badge.style.cssText = n
    ? 'background:var(--accent);color:#fff;border-radius:8px;padding:0 5px;font-size:10px;margin-left:3px'
    : '';
  $('#btnFilter').classList.toggle('on', n > 0);

  // 统计条上的筛选提示
  const parts = [];
  if (S.filters.status !== 'all') parts.push('状态=' + (STATUS_CN[S.filters.status] || S.filters.status));
  S.filters.tags.forEach((t) => parts.push('标签=' + t));
  Object.entries(S.filters.facets).forEach(([k, v]) => { if (v) parts.push(k + '=' + v); });
  (S.filters.special || []).forEach((s) => parts.push(SPECIAL_CN[s] || s));
  if (S.filters.prevIds) parts.push('只看上轮打回');
  if ((S.filters.search || '').trim()) parts.push('搜索“' + S.filters.search.trim() + '”');
  $('#activeFilters').innerHTML = parts.length
    ? '筛选中：' + parts.map(esc).join(' · ') + ' <button class="sm" id="clearFilters">清除筛选</button>'
    : '';
  const clearBtn = $('#clearFilters');
  if (clearBtn) clearBtn.addEventListener('click', resetFilters);

  // 快捷状态按钮高亮
  $$('#quickFilters button').forEach((b) => b.classList.toggle('on', b.dataset.status === S.filters.status));
}

function resetFilters() {
  S.filters = { status: 'all', tags: new Set(), facets: {}, search: '', special: new Set(), prevIds: null };
  $('#searchBox').value = '';
  $('#fSearch').value = '';
  $$('#fStatus button').forEach((b) => b.classList.toggle('on', b.dataset.status === 'all'));
  $$('#fTags .tag').forEach((el) => el.classList.remove('on'));
  $$('#fSpecial .tag').forEach((el) => el.classList.remove('on'));
  renderFacetInputs();
  applyFilters();
  renderAll();
}

function renderFacetInputs() {
  const facets = (S.set && S.set.facets) || {};
  const fields = Object.keys(facets);
  if (!fields.length) {
    $('#fFacets').innerHTML = '<span class="empty-hint">该素材集没有可用的属性分面</span>';
    return;
  }
  $('#fFacets').innerHTML = fields.map((field) => {
    const values = facets[field];
    const current = S.filters.facets[field] || '';
    const options = Object.keys(values).map((v) =>
      '<option value="' + esc(v) + '"' + (v === current ? ' selected' : '') + '>' +
      esc(v) + '（' + values[v] + '）</option>').join('');
    return '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">' +
      '<span style="min-width:70px;color:var(--text-dim);font-size:12px">' + esc(field) + '</span>' +
      '<select data-facet="' + esc(field) + '" style="flex:1">' +
      '<option value="">（不限）</option>' + options + '</select></div>';
  }).join('');
}

/* ============================================================
   渲染总入口
   ============================================================ */

function renderAll() {
  renderStats();
  if (S.view === 'detail') renderDetail(); else renderWall();
}

function setView(view) {
  S.view = view;
  $$('#viewSeg button').forEach((b) => b.classList.toggle('on', b.dataset.view === view));
  $('#detailView').classList.toggle('hidden', view !== 'detail');
  $('#wallView').classList.toggle('active', view === 'wall');
  if (view === 'wall') renderWall(); else renderDetail();
}

/* ------------------------------------------------------------ 统计 */

function renderStats() {
  const total = S.items.length;
  let pass = 0, reject = 0, flag = 0, stale = 0;
  S.items.forEach((it) => {
    const rec = S.statuses[it.key] || {};
    const st = rec.s || '';
    if (st === 'pass') pass++;
    else if (st === 'reject') reject++;
    else if (st === 'flag') flag++;
    if (rec.x) stale++;
  });
  const reviewed = pass + reject + flag;
  const unreviewed = total - reviewed;
  S.stats = Object.assign({}, S.stats, {
    total, pass, reject, flag, reviewed, unreviewed, stale,
  });

  const pct = (n) => (total ? (n / total * 100) : 0);
  // 进度条是「计量分布」不是「状态强调」（R3 共识·红色预算）：用墨色明度区分，
  // 满饱和印章红只留给判定动作与印章。通过=深墨、打回=中墨、存疑=浅墨。
  $('#progressTrack').innerHTML =
    '<div style="width:' + pct(pass) + '%;background:var(--ink)"></div>' +
    '<div style="width:' + pct(reject) + '%;background:var(--ink-2)"></div>' +
    '<div style="width:' + pct(flag) + '%;background:var(--hairline)"></div>';

  $('#progressText').innerHTML = '已审 <b>' + reviewed + '</b> / ' + total +
    '（' + (total ? Math.round(pct(reviewed)) : 0) + '%）';

  // 快捷筛选按钮上直接写清条数，一眼就知道还剩多少没审
  const quick = {
    'all': total, '': unreviewed, pass: pass, reject: reject, flag: flag,
  };
  $$('#quickFilters button').forEach((btn) => {
    const key = btn.dataset.status;
    const count = quick[key] === undefined ? '' : quick[key];
    const label = { all: '全部', '': '未审', pass: '通过', reject: '打回', flag: '存疑' }[key] || key;
    const icon = { pass: Icons.get('check', 13), reject: Icons.get('x', 13), flag: Icons.get('alertTriangle', 13) }[key] || '';
    btn.innerHTML = icon + ' ' + label + ' <b style="opacity:.75">' + count + '</b>';
  });

  $('#countStats').innerHTML =
    '<span class="stat-item">当前筛选 <b>' + S.filtered.length + '</b> 条</span>';
  if (stale) {
    $('#countStats').innerHTML +=
      '<span class="stat-item" style="color:var(--flag)">源文件已变 <b>' + stale + '</b></span>';
  }

  renderStaleBar(stale);
  renderRoundHint();

  // 有源文件变过的条目时，在筛选条上直接给一个入口
  const quickWrap = $('#quickFilters');
  let chip = quickWrap.querySelector('.review-chip');
  if (stale) {
    if (!chip) {
      chip = document.createElement('button');
      chip.className = 'sm review-chip';
      chip.title = '只看源文件被改过、结论可能过期的那些';
      chip.addEventListener('click', () => {
        S.filters.special = new Set(['stale']);
        applyFilters();
        renderAll();
      });
      quickWrap.appendChild(chip);
    }
    chip.innerHTML = Icons.get('alertTriangle', 13) + ' 需复审 <b>' + stale + '</b>';
    chip.classList.toggle('on', (S.filters.special || new Set()).has('stale'));
  } else if (chip) {
    chip.remove();
  }

  // 批量按钮直接标出会作用到多少条，避免「以为只影响当前页」
  const target = batchTarget();
  const suffix = '（' + target.keys.length + '）';
  $('#batchPass').textContent = '全部通过' + suffix;
  $('#batchFlag').textContent = '全部存疑' + suffix;
  $('#batchReject').textContent = '全部打回…' + suffix;
  $('#batchSummary').textContent = '将作用于' + target.desc + (S.selected.size ? '' : '（当前筛选结果）');
  $('#wallBatchPass').textContent = '全部通过' + suffix;
  $('#wallBatchFlag').textContent = '全部存疑' + suffix;
  $('#wallBatchReject').textContent = '全部打回…' + suffix;

  $('#posLabel').textContent = S.filtered.length
    ? (S.cursor + 1) + ' / ' + S.filtered.length : '— / —';
}

function renderStaleBar(stale) {
  const count = stale === undefined ? (S.stats.stale || 0) : stale;
  const bar = $('#staleBar');
  $('#staleCount').textContent = count;
  bar.classList.toggle('show', count > 0 && !S.staleDismissed);
}

function renderRoundHint() {
  const round = S.stats.round || 1;
  let text = '当前是第 <b>' + round + '</b> 轮。归档后本轮结果会存成单独文件，当前批次清空重审。';
  if (S.filters.prevIds) text = '正在只看<b>上一轮打回</b>的 ' + S.filters.prevIds.size + ' 条。';
  $('#roundHint').innerHTML = text;
}

/* ============================================================
   单张对照视图
   ============================================================ */

async function getItem(key) {
  if (S.itemCache.has(key)) return S.itemCache.get(key);
  const data = await apiGet('/api/set/' + encodeURIComponent(S.set.id) + '/item/' + encodeURIComponent(key));
  S.itemCache.set(key, data.item);
  if (S.itemCache.size > 60) {
    const firstKey = S.itemCache.keys().next().value;
    S.itemCache.delete(firstKey);
  }
  return data.item;
}

function fileUrl(key, side) {
  return '/api/set/' + encodeURIComponent(S.set.id) + '/file/' + encodeURIComponent(key) + '/' + side;
}

function recordOf(key) { return S.statuses[key] || { s: '', t: [], n: 0, c: 0, x: 0 }; }

function itemByKey(key) { return S.itemByKey.get(key) || null; }

/** 图片 URL 带上源文件指纹：源文件被重渲后 URL 变化，浏览器一定重新拉新图 */
function fileUrl(itemOrKey, side) {
  const item = typeof itemOrKey === 'string' ? itemByKey(itemOrKey) : itemOrKey;
  const key = item ? item.key : itemOrKey;
  const version = item && item.v ? '?v=' + item.v : '';
  return '/api/set/' + encodeURIComponent(S.set.id) + '/file/' +
    encodeURIComponent(key) + '/' + side + version;
}

function thumbUrl(item, side, size) {
  return '/api/set/' + encodeURIComponent(S.set.id) + '/thumb/' +
    encodeURIComponent(item.key) + '/' + side + '?size=' + size +
    (item.v ? '&v=' + item.v : '');
}

async function renderDetail() {
  renderStats();
  if (!S.filtered.length) {
    S.cur = null; S.draft = null;
    $('#itemMeta').innerHTML = '<span class="empty-hint">没有符合条件的素材</span>';
    $('#tagChips').querySelectorAll('.tag').forEach((el) => el.classList.remove('on'));
    $('#noteInput').value = '';
    $('#annotList').innerHTML = '';
    if (S.set.type === 'image') {
      showEmpty('a', ''); showEmpty('b', '没有符合条件的素材');
    } else {
      $('#textA').textContent = ''; $('#textB').textContent = '';
    }
    return;
  }

  const item = S.items[S.filtered[S.cursor]];
  S.cur = item;
  S.lastNote = '';
  S.draft = { status: '', tags: [], note: '', annotations: [] };
  paintDraftControls();
  paintMeta(item, {});

  let full;
  try {
    full = await getItem(item.key);
  } catch (err) {
    toast('加载素材失败：' + err.message, { error: true });
    return;
  }
  if (!S.cur || S.cur.key !== item.key) return;  // 用户已经翻走了
  S.curFull = full;
  const record = full.record || {};
  S.draft.status = record.status || '';
  S.draft.tags = (record.tags || []).slice();
  S.draft.note = record.note || '';
  S.draft.annotations = record.annotations || [];
  S.lastNote = S.draft.note;
  paintDraftControls();
  paintMeta(item, full);

  if (S.set.type === 'image') renderImagePanes(full);
  else if (S.set.type === 'model') renderModelPanesLazy(full);
  else renderTextPanes(full);
  renderAnnotList();
  prefetchNeighbors();
}

function paintDraftControls() {
  $$('#statusBtns button').forEach((b) => b.classList.toggle('on', b.dataset.status === S.draft.status));
  const tagSet = new Set(S.draft.tags);
  $('#tagChips').querySelectorAll('.tag').forEach((el) => el.classList.toggle('on', tagSet.has(el.dataset.tag)));
  const note = $('#noteInput');
  if (document.activeElement !== note) note.value = S.draft.note || '';
  $('#statBadge').innerHTML = S.draft.status
    ? '状态：<b style="color:var(--' + S.draft.status + ')">' + STATUS_CN[S.draft.status] + '</b>'
    : '状态：未审';
  $('#textStatBadge').innerHTML = $('#statBadge').innerHTML;
  // 侧栏「素材信息」里的审批状态/标签也要跟着变，否则打完标签它还显示未审
  if (S.cur) paintMeta(S.cur, S.curFull);
}

function paintMeta(item, full) {
  // html:true 的条目是自己拼好的标记，不能再转义一次
  const rows = [];
  rows.push({ k: '名称', v: item.name });
  rows.push({ k: 'id', v: '<span class="mono">' + esc(item.id) + '</span>', html: true });
  if (item.group) rows.push({ k: '分组', v: item.group });
  Object.entries(item.meta || {}).forEach(([k, v]) => { if (v) rows.push({ k: k, v: v }); });
  if (S.draft) {
    rows.push({
      k: '审批状态',
      v: S.draft.status
        ? '<b style="color:var(--' + S.draft.status + ')">' + STATUS_CN[S.draft.status] + '</b>' : '未审',
      html: true,
    });
    if (S.draft.tags.length) rows.push({ k: '问题标签', v: S.draft.tags.join('、') });
    if (S.draft.annotations.length) rows.push({ k: '圈选', v: S.draft.annotations.length + ' 处' });
  }
  // 源文件在审批之后被改过：这里必须显眼，否则用户会沿用已经过期的结论
  if (S.cur && recordOf(S.cur.key).x) {
    rows.push({
      k: '源文件',
      v: '<span style="color:var(--flag)">' + Icons.get('alertTriangle', 13) + ' 审批后已被改过，此结论可能过期</span> '
         + '<button class="sm" id="btnReReview" style="margin-top:2px">清除结论，重新审</button>',
      html: true,
    });
  }
  $('#itemMeta').innerHTML = rows.map((row) =>
    '<dt>' + esc(row.k) + '</dt><dd>' + (row.html ? row.v : esc(row.v)) + '</dd>').join('');

  const infoA = full && full.a ? full.a : null;
  const infoB = full && full.b ? full.b : null;
  // 文件路径收进 tooltip（评审：完整路径是调试信息，审批者只需知道是哪一侧）
  const nameA = infoA && infoA.path ? String(infoA.path).split(/[\\/]/).pop() : '';
  const nameB = infoB && infoB.path ? String(infoB.path).split(/[\\/]/).pop() : '';
  $('#metaA').textContent = nameA; $('#metaA').title = infoA && infoA.path ? shortPath(infoA.path) : '';
  $('#metaB').textContent = nameB; $('#metaB').title = infoB && infoB.path ? shortPath(infoB.path) : '';
}

/** 切换素材集时把单张视图里的残留内容清掉，避免看到上一个素材集的信息 */
function clearDetailPane() {
  S.cur = null;
  S.curFull = null;
  S.draft = null;
  $('#itemMeta').innerHTML = '<span class="empty-hint">请在缩略图墙里选一张素材</span>';
  $('#annotList').innerHTML = '';
  $('#annotCount').textContent = '';
  $('#noteInput').value = '';
  $('#textA').textContent = '';
  $('#textB').textContent = '';
  $('#metaA').textContent = '';
  $('#metaB').textContent = '';
  $('#statBadge').innerHTML = '';
  $('#textStatBadge').innerHTML = '';
  $$('#statusBtns button').forEach((b) => b.classList.remove('on'));
  $$('#tagChips .tag').forEach((el) => el.classList.remove('on'));
}

function shortPath(path) {
  const parts = String(path).split(/[\\/]/);
  return parts.slice(-2).join('/');
}

/* ------------------------------------------------------------ 图片对照 */

/** model 类型的 3D 预览。model.js 是普通脚本、自包含（不依赖 import map），
 *  挂到 window.ModelPreview 上。加载失败时降级为元信息，不影响其它视图。 */
function renderModelPanesLazy(item) {
  if (window.ModelPreview && typeof window.ModelPreview.renderModelPanes === 'function') {
    window.ModelPreview.renderModelPanes(item).catch(() => { /* 错误已在 model.js 里降级 */ });
    return;
  }
  const el = document.getElementById('modelInfo');
  if (el) el.textContent = '3D 预览模块未加载';
  ['a', 'b'].forEach((side) => {
    const info = item[side] || {};
    const empty = document.getElementById('modelEmpty' + side.toUpperCase());
    if (empty) {
      empty.style.display = 'flex';
      empty.textContent = info.exists
        ? '3D 预览模块未加载。文件：' + (info.path || '')
        : (info.missing_reason || '缺少该侧文件');
    }
  });
}

function showEmpty(side, message) {
  const stage = $('#stage' + side.toUpperCase());
  const empty = $('#empty' + side.toUpperCase());
  stage.style.display = 'none';
  empty.style.display = message ? 'flex' : 'none';
  empty.textContent = message || '';
}

function renderImagePanes(item) {
  const z = S.zoom;
  z.nat = { a: null, b: null };
  z.userAdjusted = false;

  // 把舞台设成图片原始像素尺寸，圈选坐标才能按比例落在图上；
  // 舞台用 margin 负半宽居中，之后只靠 transform 缩放平移。
  const applySize = (side, img, stage) => {
    const size = { w: img.naturalWidth, h: img.naturalHeight };
    if (!size.w || !size.h) return;
    z.nat[side] = size;
    stageBox(side, size.w, size.h);
    normalizeOverlayBoxes();   // 叠加模式下两侧要画成同样大小
    tryFit();
  };

  ['a', 'b'].forEach((side) => {
    const info = item[side] || {};
    if (!info.exists) {
      showEmpty(side, (info.missing_reason || '缺少该侧图片') + '\n' + S.set['source' + side.toUpperCase()]);
      if (S.overlay.on) {
        setOverlayMode(false);
        toast('这张缺一侧图片，已退出叠加对比', { error: true });
      }
      return;
    }
    const stage = $('#stage' + side.toUpperCase());
    const img = $('#img' + side.toUpperCase());
    const empty = $('#empty' + side.toUpperCase());
    empty.style.display = 'none';
    stage.style.display = '';

    const onLoad = () => applySize(side, img, stage);
    img.onload = onLoad;
    img.onerror = () => showEmpty(side, '图片读取失败：' + shortPath(info.path || ''));

    const url = fileUrl(item, side);
    if (img.getAttribute('src') !== url) {
      stage.style.width = '';
      stage.style.height = '';
      img.setAttribute('src', url);
      if (img.complete && img.naturalWidth) onLoad();   // 命中缓存时 onload 可能已经错过
    } else if (img.complete && img.naturalWidth) {
      onLoad();
    }
  });

  // 布局稳定后再兜一次（首帧视口高度可能还没定下来）
  requestAnimationFrame(() => {
    ['a', 'b'].forEach((side) => {
      const img = $('#img' + side.toUpperCase());
      const stage = $('#stage' + side.toUpperCase());
      if (img && stage && img.complete && img.naturalWidth && !z.nat[side]) applySize(side, img, stage);
    });
    drawAnnotations();
  });
}

function viewportFor(side) { return $('#viewport' + side.toUpperCase()); }

/** 当前真正在显示图片的那个视口（叠加模式下是叠加视口），缩放平移都按它算 */
function activeViewport() {
  return (S.overlay.on && $('#viewportO')) ? $('#viewportO') : $('#viewportB');
}

/* ------------------------------------------------------------ 叠加对比 */

function stageBox(side, w, h) {
  const stage = $('#stage' + side.toUpperCase());
  stage.style.width = w + 'px';
  stage.style.height = h + 'px';
  stage.style.marginLeft = (-w / 2) + 'px';
  stage.style.marginTop = (-h / 2) + 'px';
}

/** 叠加时把两侧都缩放到同一个视口尺寸（取两边能完整显示的最大公共尺寸）。
 *  原图和成品分辨率不一样时（比如 819 vs 946），如果直接按某一侧的尺寸画，
 *  另一侧会显示不全或一大一小，左右对比就对不上。
 *  统一按「两边都装得下」的尺寸等比缩放，圈选坐标是按比例存的，缩放不影响。 */
function normalizeOverlayBoxes() {
  if (!S.overlay.on) return;
  const imgA = $('#imgA'), imgB = $('#imgB');
  const na = { w: imgA.naturalWidth || 0, h: imgA.naturalHeight || 0 };
  const nb = { w: imgB.naturalWidth || 0, h: imgB.naturalHeight || 0 };
  if (!na.w || !nb.w) return;
  // 用两边里较小的那个"视口高度"，宽度按比例算，保证两边都能完整显示
  const h = Math.max(na.h, nb.h);
  const wa = Math.round(na.w * h / na.h);
  const wb = Math.round(nb.w * h / nb.h);
  stageBox('a', wa, h);
  stageBox('b', wb, h);
}

function setOverlayMode(on) {
  const panes = $('#panes');
  const overlayPane = $('#overlayPane');
  if (on) {
    // 两侧都得有图，叠加才有意义
    const item = S.cur ? itemByKey(S.cur.key) : null;
    if (!item || !item.hasA || !item.hasB) {
      toast('这张素材缺一侧图片，没法叠加对比', { error: true });
      return;
    }
    if (S.annotating) toggleAnnotate();
    // 先把 #stageB 放进 #viewportO，再把 #ovClip（装源A）append 到 #viewportO 末尾。
    // #stageB 有 transform 是独立 stacking context，DOM 顺序决定 z 序；
    // 让 #ovClip 排在 #stageB 后面，左半边的源A 才不会被源B 整屏盖住。
    $('#viewportO').appendChild($('#stageB'));
    $('#ovClip').appendChild($('#stageA'));
    $('#viewportO').appendChild($('#ovClip'));
    panes.classList.add('overlay-mode');
    // HTML 里给 overlayPane 写了内联 display:none，内联样式优先级高于类选择器，
    // 所以这里必须显式切换，不能只靠 CSS 规则。
    overlayPane.style.display = 'flex';
    S.overlay.on = true;
    S.overlay.mode = S.overlay.mode || 'slider';
    S.overlay.alpha = S.overlay.alpha === undefined ? 0.5 : S.overlay.alpha;
    // 等比缩放到同一视口尺寸，保证左右对得上
    normalizeOverlayBoxes();
    // 叠加模式默认滑块对比，把分界放中间
    S.overlay.divider = 50;
    applyOverlay();
  } else {
    // 退出时把 #ovClip 放回 #viewportO 里的原位（在 #ovLabelL 前面），
    // 免得下次进入叠加时它还在最后、又把源A 盖住
    const viewportO = $('#viewportO');
    const ovClip = $('#ovClip');
    if (ovClip && ovClip.parentElement === viewportO) {
      viewportO.insertBefore(ovClip, $('#ovLabelL'));
    }
    $('#viewportA').appendChild($('#stageA'));
    $('#viewportB').appendChild($('#stageB'));
    panes.classList.remove('overlay-mode');
    overlayPane.style.display = 'none';
    $('#viewportO').classList.remove('blink', 'blend');
    S.overlay.on = false;
    // 退出叠加后各自恢复原始像素尺寸
    ['a', 'b'].forEach((side) => {
      const img = $('#img' + side.toUpperCase());
      if (img.naturalWidth) stageBox(side, img.naturalWidth, img.naturalHeight);
    });
  }
  $('#btnOverlay').classList.toggle('on', S.overlay.on);
  $('#btnAnnotate').disabled = S.overlay.on;
  applyOverlay();
  fitNow();
  drawAnnotations();
  if (S.overlay.on) toast('叠加对比：拖中间竖线分界，按住空格键看全幅原图', { duration: 4000 });
}

function applyOverlay() {
  if (!S.overlay.on) return;
  const mode = S.overlay.mode || 'slider';
  const vp = $('#viewportO');
  vp.classList.toggle('blend', mode === 'blend');
  if (mode === 'slider') {
    const divider = S.overlay.divider;
    $('#ovClip').style.clipPath = 'inset(0 ' + (100 - divider) + '% 0 0)';
    $('#ovDivider').style.left = divider + '%';
  } else {
    // 半透明叠加：源B 整体盖在源A 上，透明度由滑块控制
    // 先把滑块模式的 clip-path 清掉，不然源A 只剩左半边
    $('#ovClip').style.clipPath = '';
    const alpha = clamp(S.overlay.alpha === undefined ? 0.5 : S.overlay.alpha, 0, 1);
    vp.style.setProperty('--blend-alpha', alpha);
    $('#blendAlpha').value = Math.round(alpha * 100);
    $('#blendAlphaVal').textContent = Math.round(alpha * 100) + '%';
  }
  $$('#overlayModeSeg button').forEach((b) => b.classList.toggle('on', b.dataset.mode === mode));
  $('#overlayHint').textContent = mode === 'slider'
    ? '拖动中间竖线分界；按住空格键临时看全幅源A'
    : '拖动下方滑块改变源B 的透明度；按住空格键临时只看源A';
}

function blinkOverlay(on) {
  if (!S.overlay.on) return;
  const vp = $('#viewportO');
  if (S.overlay.mode === 'blend') {
    // 半透明模式下按住空格 = 暂时把源B 透明度拉到 0，只看源A
    vp.classList.toggle('blink-hide-b', on);
  } else {
    vp.classList.toggle('blink', on);
  }
}

function bindOverlayViewport() {
  const vp = $('#viewportO');
  let panning = false, startX = 0, startY = 0;

  vp.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    if (e.target.closest('#ovDivider, .blend-slider')) return;   // 分隔线和透明度滑块有自己的交互
    panning = true;
    startX = e.clientX; startY = e.clientY;
    vp.classList.add('dragging');
    e.preventDefault();
  });
  window.addEventListener('mousemove', (e) => {
    if (!panning) return;
    S.zoom.tx += e.clientX - startX;
    S.zoom.ty += e.clientY - startY;
    S.zoom.userAdjusted = true;
    startX = e.clientX; startY = e.clientY;
    applyTransform();
  });
  window.addEventListener('mouseup', () => {
    if (!panning) return;
    panning = false;
    vp.classList.remove('dragging');
  });
  vp.addEventListener('wheel', (e) => {
    e.preventDefault();
    zoomAt(e.clientX, e.clientY, e.deltaY < 0 ? 1.15 : 1 / 1.15);
  }, { passive: false });
  vp.addEventListener('dblclick', () => fitNow());

  // 分隔线拖动
  const divider = $('#ovDivider');
  let dragging = false;
  divider.addEventListener('mousedown', (e) => {
    e.preventDefault();
    e.stopPropagation();
    dragging = true;
  });
  window.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const rect = vp.getBoundingClientRect();
    S.overlay.divider = clamp((e.clientX - rect.left) / rect.width * 100, 1, 99);
    applyOverlay();
  });
  window.addEventListener('mouseup', () => { dragging = false; });
}

function tryFit() {
  const z = S.zoom;
  if (z.userAdjusted) return;
  if (!z.nat || (!z.nat.a && !z.nat.b)) return;
  doFit(z.nat);
}

function doFit(nat) {
  const vp = activeViewport();
  const vw = vp.clientWidth - 16, vh = vp.clientHeight - 16;
  const sizes = [];
  if (nat.a) sizes.push(nat.a);
  if (nat.b) sizes.push(nat.b);
  if (!sizes.length || vw <= 0 || vh <= 0) return;
  let scale = Infinity;
  sizes.forEach((s) => { scale = Math.min(scale, vw / s.w, vh / s.h); });
  S.zoom.fit = scale;
  S.zoom.scale = scale;
  S.zoom.tx = 0; S.zoom.ty = 0;
  applyTransform();
}

function fitNow() {
  const z = S.zoom;
  z.userAdjusted = false;
  if (z.nat && (z.nat.a || z.nat.b)) {
    doFit(z.nat);
  } else {
    const img = $('#imgB');
    if (img && img.naturalWidth) {
      doFit({ a: { w: img.naturalWidth, h: img.naturalHeight }, b: { w: img.naturalWidth, h: img.naturalHeight } });
    } else {
      z.scale = 1; z.tx = 0; z.ty = 0; applyTransform();
    }
  }
}

function applyTransform() {
  const t = 'translate(' + S.zoom.tx + 'px,' + S.zoom.ty + 'px) scale(' + S.zoom.scale + ')';
  ['#stageA', '#stageB'].forEach((sel) => { $(sel).style.transform = t; });
  $('#zoomLabel').textContent = Math.round(S.zoom.scale * 100) + '%';
}

function zoomAt(clientX, clientY, factor) {
  const vp = activeViewport();
  const rect = vp.getBoundingClientRect();
  const px = clientX - (rect.left + rect.width / 2);
  const py = clientY - (rect.top + rect.height / 2);
  const s0 = S.zoom.scale;
  const s1 = clamp(s0 * factor, 0.03, 20);
  if (s1 === s0) return;
  S.zoom.tx = px - (px - S.zoom.tx) * (s1 / s0);
  S.zoom.ty = py - (py - S.zoom.ty) * (s1 / s0);
  S.zoom.scale = s1;
  S.zoom.userAdjusted = true;
  applyTransform();
}

/* ------------------------------------------------------------ 圈图标注 */

function stageMetrics(side) {
  const stage = $('#stage' + side.toUpperCase());
  const img = $('#img' + side.toUpperCase());
  if (!stage || !img || !img.naturalWidth) return null;
  const rect = stage.getBoundingClientRect();
  return { rect, natW: img.naturalWidth, natH: img.naturalHeight, scale: S.zoom.scale };
}

function stageToNormalized(side, clientX, clientY) {
  const m = stageMetrics(side);
  if (!m) return null;
  return {
    x: clamp((clientX - m.rect.left) / m.scale / m.natW, 0, 1),
    y: clamp((clientY - m.rect.top) / m.scale / m.natH, 0, 1),
  };
}

function drawAnnotations() {
  if (S.set.type !== 'image') return;
  ['a', 'b'].forEach((side) => {
    const overlay = $('#overlay' + side.toUpperCase());
    const list = (S.draft && S.draft.annotations || []).filter((an) => an.side === side);
    overlay.innerHTML = list.map((an) => {
      const idx = S.draft.annotations.indexOf(an) + 1;
      const active = (idx - 1) === S.annotFocus ? ' active' : '';
      return '<div class="annot side-' + side + active + '" data-annot="' + (idx - 1) + '"' +
        ' style="left:' + (an.x * 100) + '%;top:' + (an.y * 100) + '%;width:' + (an.w * 100) + '%;height:' + (an.h * 100) + '%">' +
        '<span class="num">' + idx + '</span></div>';
    }).join('');
  });
}

function renderAnnotList() {
  const list = (S.draft && S.draft.annotations) || [];
  const isText = S.set.type === 'text';
  $('#annotCount').textContent = list.length ? '（' + list.length + ' 处）' : '';
  $('#annotHint').innerHTML = S.draft
    ? (isText
      ? '行标记：点文本行首的「行号」，选 漏译 / 术语错。会自动保存。'
      : '画框：点工具栏「圈图」或按 R，然后在图上拖拽。框内文字在下面填写，会自动保存。')
    : '';

  const ul = $('#annotList');
  if (!list.length) {
    ul.innerHTML = '<li style="cursor:default;background:transparent;border:none;padding:0" class="empty-hint">'
      + (isText ? '还没有行标记' : '还没有圈选') + '</li>';
    return;
  }
  ul.innerHTML = list.map((an, i) => {
    const sideName = an.side === 'a' ? S.set.sourceA : S.set.sourceB;
    // 文本态行标记：x 存行号（1 起），y/w/h 恒为 0，显示行号而非百分比坐标
    const isLineMark = an.w === 0 && an.h === 0 && an.y === 0 && Number(an.x) >= 1;
    const pos = isLineMark
      ? '第 ' + Math.round(Number(an.x)) + ' 行' + (an.tag ? ' · ' + an.tag : '')
      : 'x' + (an.x * 100).toFixed(0) + '% y' + (an.y * 100).toFixed(0) + '% ' +
        (an.w * 100).toFixed(0) + '×' + (an.h * 100).toFixed(0) + '%';
    return '<li data-annot="' + i + '" class="' + (i === S.annotFocus ? 'active' : '') + '">' +
      '<span class="idx">' + (i + 1) + '</span>' +
      '<div class="body">' +
      '<input type="text" class="annot-note" data-annot-note="' + i + '" placeholder="说明这个问题…" ' +
      'value="' + esc(an.note || '') + '" style="width:100%;margin-bottom:3px">' +
      '<div class="sub">' + esc(sideName) + ' · ' + esc(pos) + '</div>' +
      '</div>' +
      '<span class="del" data-annot-del="' + i + '" title="删除">' + Icons.get('x', 12) + '</span></li>';
  }).join('');

  if (S.annotFocus >= 0) {
    const input = ul.querySelector('[data-annot-note="' + S.annotFocus + '"]');
    if (input && S.annotFocusPending) {
      S.annotFocusPending = false;
      // addAnnotation 里已经把侧栏的 transition 暂时禁用并强制可见，
      // 这里同步 focus 即可生效；不再需要 setTimeout/rAF 等待动画。
      input.focus();
      input.select();
    }
  }
}

function addAnnotation(side, box) {
  if (!S.draft) return;
  S.annotFocus = S.draft.annotations.length;
  S.annotFocusPending = true;
  S.draft.annotations.push({ side: side, x: box.x, y: box.y, w: box.w, h: box.h, note: '', tag: '' });
  drawAnnotations();
  // 先展开侧栏，再渲染列表并聚焦——visibility:hidden 时 focus 不生效。
  // 侧栏有 180ms 的 transform/visibility 过渡，圈选后要立即可输入，
  // 因此打开时先禁用 transition 让它立刻可见，focus 完成后再恢复过渡，
  // 避免「focus 时元素还 visibility:hidden → focus 静默失败」。
  const panel = $('#sidePanel');
  const needOpen = !panel.classList.contains('open');
  if (needOpen) {
    S.annotAutoOpened = true;
    const origTransition = panel.style.transition;
    panel.style.transition = 'none';
    toggleSidePanel(true);
    // 强制 reflow，让 transition:none 立即生效
    void panel.offsetHeight;
    panel.style.transition = origTransition;
  }
  renderAnnotList();
  flush();
}

function updateAnnotation(index, patch) {
  if (!S.draft || !S.draft.annotations[index]) return;
  Object.assign(S.draft.annotations[index], patch);
  drawAnnotations();
  renderAnnotList();
  flush();
}

function deleteAnnotation(index) {
  if (!S.draft) return;
  S.draft.annotations.splice(index, 1);
  if (S.annotFocus >= S.draft.annotations.length) S.annotFocus = S.draft.annotations.length - 1;
  drawAnnotations();
  renderAnnotList();
  flush();
}

function focusAnnotation(index) {
  if (!S.draft || !S.draft.annotations[index]) return;
  S.annotFocus = index;
  const an = S.draft.annotations[index];
  // 文本态行标记：滚动到该行并闪一下，没有画布可平移
  const isLineMark = an.w === 0 && an.h === 0 && an.y === 0 && Number(an.x) >= 1;
  if (isLineMark) {
    const body = $('#text' + (an.side || 'b').toUpperCase());
    const row = body && body.querySelector('.ln-row[data-line="' + Math.round(Number(an.x)) + '"]');
    if (row) {
      row.scrollIntoView({ block: 'center', behavior: 'smooth' });
      row.classList.add('flash');
      setTimeout(() => row.classList.remove('flash'), 900);
    }
    renderAnnotList();
    return;
  }
  // 把视图平移过去，让该框居中
  const m = stageMetrics(an.side);
  if (m) {
    const cx = (an.x + an.w / 2) * m.natW;
    const cy = (an.y + an.h / 2) * m.natH;
    S.zoom.tx = -(cx - m.natW / 2) * S.zoom.scale;
    S.zoom.ty = -(cy - m.natH / 2) * S.zoom.scale;
    applyTransform();
  }
  drawAnnotations();
  renderAnnotList();
}

/* ------------------------------------------------------------ 文本对照 */

/* ------------------------------------------------------------ 文本行级对照（P0）
   文本态复用 record.annotations 存「问题行锚点」：x = 行号（1 起），
   tag 用预设词表（漏译/术语错…），note 是问题说明。图像态语义不变。 */

const TEXT_LINE_TAGS = ['漏译', '术语错'];

/** 把纯文本拆成 [{n, t}]，空行保留（占位对齐用） */
function splitTextLines(text) {
  return String(text || '').split('\n').map((t, i) => ({ n: i + 1, t: t }));
}

/** 从 draft.annotations 里抽出文本态的行锚点：{ lineNo -> [{tag,note,side}] } */
function textLineMarks() {
  const map = new Map();
  if (!S.draft || !S.draft.annotations) return map;
  S.draft.annotations.forEach((an) => {
    const line = Math.round(Number(an.x) || 0);
    if (line < 1) return;
    if (!map.has(line)) map.set(line, []);
    map.get(line).push(an);
  });
  return map;
}

/** 该行最严重的问题级别：miss(漏译) > term(术语错) > '' */
function lineSeverity(marks) {
  if (!marks || !marks.length) return '';
  const tags = marks.map((m) => m.tag || '');
  if (tags.some((t) => t === '漏译')) return 'miss';
  if (tags.some((t) => t === '术语错')) return 'term';
  return 'other';
}

/** 渲染一侧文本为行级结构。side: 'a'|'b'，lines: [{n,t}]，marks: Map */
function renderTextSide(lines, marks, side) {
  const maxN = lines.length;
  return lines.map((ln) => {
    const lineMarks = marks.get(ln.n) || [];
    const sev = lineSeverity(lineMarks.filter((m) => (m.side || 'b') === side));
    const cls = ['ln-row'];
    if (sev) cls.push('sev-' + sev);
    if (ln.n % 2 === 0) cls.push('alt');
    if (ln.pad) cls.push('pad');  // 补空行（diff 的 blank line marker）
    let missChip = '';
    let ltContent = ln.t ? esc(ln.t) : ' ';
    // 行内问题标记（只画在本侧）。漏译 chip 放进 .lt 紧跟文本/虚线；术语错在词上 wavy。
    lineMarks.filter((m) => (m.side || 'b') === side).forEach((m) => {
      if (m.tag === '漏译') {
        const tip = '漏译' + (m.note ? '：' + m.note : '') + '（源第 ' + ln.n + ' 行）';
        missChip = '<span class="lmiss" title="' + esc(tip) + '">漏译</span>';
      } else if (m.tag === '术语错' && m.note) {
        // 术语错：note 里写「错词=>正词」，把错词标出来
        const parts = m.note.split('=>');
        const wrong = parts[0] ? parts[0].trim() : '';
        if (wrong && ln.t.includes(wrong)) {
          ltContent = esc(ln.t).replace(esc(wrong), '<u class="lterm" title="' + esc(m.note) + '">' + esc(wrong) + '</u>');
        }
      }
    });
    let inner = '<i class="ln">' + ln.n + '</i><span class="lt">' + ltContent + missChip + '</span>';
    return '<div class="' + cls.join(' ') + '" data-line="' + ln.n + '" data-side="' + side + '">' + inner + '</div>';
  }).join('');
}

function renderTextPanes(item) {
  const paneA = item.a || {}, paneB = item.b || {};
  const bodyA = $('#textA'), bodyB = $('#textB');
  applyTextStyle();
  const marks = textLineMarks();
  const linesA = paneA.exists ? splitTextLines(paneA.text) : [];
  const linesB = paneB.exists ? splitTextLines(paneB.text) : [];
  // T3 行级同步：两侧行数对齐到较多的一侧，缺的一侧补空行占位。
  // 补空行标 pad:true（diff 工具的 blank line marker），渲染成浅虚线暗示「此处应有内容，缺」。
  const maxLines = Math.max(linesA.length, linesB.length);
  while (linesA.length < maxLines) linesA.push({ n: linesA.length + 1, t: '', pad: true });
  while (linesB.length < maxLines) linesB.push({ n: linesB.length + 1, t: '', pad: true });

  if (paneA.exists) {
    bodyA.innerHTML = renderTextSide(linesA, marks, 'a');
    bodyA.style.color = '';
  } else {
    bodyA.textContent = '（' + (paneA.missing_reason || '源A 无对应文本') + '）';
    bodyA.style.color = 'var(--text-faint)';
  }
  if (paneB.exists) {
    bodyB.innerHTML = renderTextSide(linesB, marks, 'b');
    bodyB.style.color = '';
  } else {
    bodyB.textContent = '（' + (paneB.missing_reason || '源B 无对应文本') + '）';
    bodyB.style.color = 'var(--text-faint)';
  }
  // 栏头只留「英文原文 / 中文译文」，字数收进 tooltip（P8：字数对审批判断无帮助且单位有歧义）
  const metaA = paneA.exists ? (paneA.text || '').length + ' 字' : '';
  const metaB = paneB.exists ? (paneB.text || '').length + ' 字' : '';
  $('#textMetaA').textContent = ''; $('#textMetaA').title = metaA;
  $('#textMetaB').textContent = ''; $('#textMetaB').title = metaB;
  bodyA.scrollTop = 0; bodyB.scrollTop = 0;
  bindTextLineHover();
}

/** 行悬浮联动：鼠标悬停某侧某行时，另一侧同一行号高亮（T3 的视觉锚） */
function bindTextLineHover() {
  ['#textA', '#textB'].forEach((sel) => {
    const el = $(sel);
    el.onmouseover = (e) => {
      const row = e.target.closest('.ln-row');
      $$('#textWrap .ln-row.hover').forEach((r) => r.classList.remove('hover'));
      if (!row) return;
      const n = row.dataset.line;
      $$('#textWrap .ln-row[data-line="' + n + '"]').forEach((r) => r.classList.add('hover'));
    };
    el.onmouseleave = () => {
      $$('#textWrap .ln-row.hover').forEach((r) => r.classList.remove('hover'));
    };
    // 点击行号：给该行加/切换问题标记（文本态的「圈选」）
    el.onclick = (e) => {
      const ln = e.target.closest('.ln');
      if (!ln) return;
      const row = ln.closest('.ln-row');
      if (!row || !S.draft) return;
      const lineNo = Number(row.dataset.line);
      const side = row.dataset.side;
      addTextLineMark(lineNo, side);
    };
  });
}

/** 给某行加一个问题标记：弹小菜单选 漏译/术语错，选完写进 draft.annotations */
function addTextLineMark(lineNo, side) {
  // 已有标记：切换/删除
  const existing = S.draft.annotations.findIndex((an) =>
    Math.round(Number(an.x) || 0) === lineNo && (an.side || 'b') === side);
  const menu = document.createElement('div');
  menu.className = 'ln-menu';
  menu.innerHTML = '<div class="ln-menu-title">第 ' + lineNo + ' 行标记</div>' +
    TEXT_LINE_TAGS.map((t) => '<button class="ln-menu-btn" data-tag="' + t + '">' + t + '</button>').join('') +
    (existing >= 0 ? '<button class="ln-menu-btn del" data-del="1">删除标记</button>' : '') +
    '<button class="ln-menu-btn cancel">取消</button>';
  document.body.appendChild(menu);
  const row = $('#text' + side.toUpperCase()).querySelector('.ln-row[data-line="' + lineNo + '"]');
  const rc = row ? row.getBoundingClientRect() : { left: 200, bottom: 200 };
  menu.style.left = Math.min(rc.left, window.innerWidth - 180) + 'px';
  menu.style.top = (rc.bottom + 6) + 'px';
  const close = () => { menu.remove(); document.removeEventListener('mousedown', onOut); };
  const onOut = (e) => { if (!menu.contains(e.target)) close(); };
  document.addEventListener('mousedown', onOut);
  menu.addEventListener('click', (e) => {
    const btn = e.target.closest('.ln-menu-btn');
    if (!btn || btn.classList.contains('cancel')) { close(); return; }
    if (btn.dataset.del) {
      S.draft.annotations.splice(existing, 1);
    } else {
      const tag = btn.dataset.tag;
      let note = '';
      if (tag === '术语错') note = prompt('术语错：错词=>正词（如「柱子=>石柱」）', '') || '';
      else note = prompt('漏译说明（可空）', '') || '';
      if (existing >= 0) S.draft.annotations.splice(existing, 1);
      S.draft.annotations.push({ side: side, x: lineNo, y: 0, w: 0, h: 0, note: note, tag: tag });
    }
    close();
    renderTextPanes(S.curFull || {});
    renderAnnotList();
    flush();
  });
}

function applyTextStyle() {
  const size = S.textFont + 'px';
  const stack = S.textStacked ? 'column' : 'row';
  $('#textWrap').style.flexDirection = stack;
  $$('#textWrap .text-pane').forEach((el) => {
    el.style.borderRight = S.textStacked ? 'none' : '';
    el.style.borderBottom = S.textStacked ? '1px solid var(--line)' : '';
  });
  ['#textA', '#textB'].forEach((sel) => {
    const el = $(sel);
    el.style.fontSize = size;
    el.classList.toggle('nowrap', !S.textWrap);
  });
}

/* ------------------------------------------------------------ 相邻预取 */

function prefetchNeighbors() {
  if (S.set.type === 'image') {
    ['a', 'b'].forEach((side) => {
      [1, 2].forEach((offset) => {
        const pos = S.cursor + offset;
        if (pos >= S.filtered.length) return;
        const it = S.items[S.filtered[pos]];
        if (!it || !it['has' + side.toUpperCase()]) return;
        const img = new Image();
        img.src = fileUrl(it, side);
      });
    });
  }
  [1, 2].forEach((offset) => {
    const pos = S.cursor + offset;
    if (pos < S.filtered.length) {
      const it = S.items[S.filtered[pos]];
      if (it && !S.itemCache.has(it.key)) getItem(it.key).catch(() => {});
    }
  });
}

/* ============================================================
   缩略图墙
   ============================================================ */

const wallObserver = new IntersectionObserver((entries) => {
  entries.forEach((entry) => {
    if (!entry.isIntersecting) return;
    const img = entry.target;
    if (img.dataset.src) { img.src = img.dataset.src; img.removeAttribute('data-src'); }
    wallObserver.unobserve(img);
  });
}, { root: $('#wallGrid'), rootMargin: '400px' });

function renderWall() {
  renderStats();
  const pageSize = S.cfg.wallPageSize || 60;
  const totalPages = Math.max(1, Math.ceil(S.filtered.length / pageSize));
  S.wallPage = clamp(S.wallPage, 1, totalPages);
  const start = (S.wallPage - 1) * pageSize;
  const slice = S.filtered.slice(start, start + pageSize);

  $('#wallInfo').textContent = '共 ' + S.filtered.length + ' 条 · 第 ' + S.wallPage + ' / ' + totalPages + ' 页';
  $('#pageInfo').textContent = S.wallPage + ' / ' + totalPages;
  $('#wallSelInfo').textContent = S.selected.size ? '已选 ' + S.selected.size + ' 条' : '';

  const grid = $('#wallGrid');
  const isImage = S.set.type === 'image';
  grid.classList.toggle('dim-reviewed', !!S.wallDim);
  grid.innerHTML = slice.map((itemIdx, i) => {
    const it = S.items[itemIdx];
    const rec = recordOf(it.key);
    const status = rec.s || '';
    const cls = ['cell', status ? 'st-' + status : ''];
    if (rec.x) cls.push('is-stale');
    if (S.selected.has(it.key)) cls.push('selected');
    const isCurrent = (start + i) === S.cursor;
    if (isCurrent) cls.push('current');

    let media;
    if (isImage) {
      const side = S.wallSide;
      const has = it['has' + side.toUpperCase()];
      media = has
        ? '<img class="thumb" data-src="' + esc(thumbUrl(it, side, S.wallCell * 2)) + '" alt="">'
        : '<div class="cell-text" style="color:var(--reject)">缺源' + side.toUpperCase() + '</div>';
    } else {
      media = '<div class="cell-text">' + esc(it.preview || '') + '</div>';
    }

    const badges = [];
    if (!it.hasA) badges.push('<span class="badge warn">缺源A</span>');
    else if (!it.hasB) badges.push('<span class="badge warn">缺源B</span>');
    if (rec.x) badges.push('<span class="badge stale" style="left:auto;right:4px">源已更新</span>');
    const annotCount = rec.c || 0;
    const tagText = (rec.t && rec.t.length ? rec.t[0] + (rec.t.length > 1 ? '+' + (rec.t.length - 1) : '') : '')
      + (annotCount ? ' ·' + annotCount + '处' : '');

    return '<div class="' + cls.join(' ') + '" data-pos="' + (start + i) + '" title="' + esc(it.id) +
      (rec.x ? '（源文件在你审批后被改过）' : '') + '">' +
      media +
      (status ? '' : '<span class="dot-new"></span>') +
      '<div class="check' + (S.selectMode ? ' show' : '') + '">' + (S.selected.has(it.key) ? '✓' : '') + '</div>' +
      (isImage && !S.selectMode
        ? '<div class="quick-annot" data-quick-annot="' + (start + i) + '" title="直接进圈注模式">' + Icons.get('pencil', 14) + '</div>' : '') +
      badges.join('') +
      '<div class="caption"><span class="cid mono">' + esc(it.id) + '</span>' +
      (tagText ? '<span class="tags">' + esc(tagText) + '</span>' : '') + '</div>' +
      '</div>';
  }).join('');

  $$('#wallGrid .cell img[data-src]').forEach((img) => wallObserver.observe(img));

  // 当前素材在墙上时滚动到可见
  const currentCell = grid.querySelector('.cell.current');
  if (currentCell && S.gotoWallScroll) {
    currentCell.scrollIntoView({ block: 'center', behavior: 'auto' });
    S.gotoWallScroll = false;
  }
}

function gotoWallPageFor(cursor) {
  const pageSize = S.cfg.wallPageSize || 60;
  S.wallPage = Math.floor(cursor / pageSize) + 1;
}

/* ============================================================
   编辑与保存
   ============================================================ */

async function flush() {
  if (!S.cur || !S.draft) return;
  const key = S.cur.key;
  const payload = {
    key: key,
    status: S.draft.status,
    tags: S.draft.tags,
    note: S.draft.note,
    annotations: S.draft.annotations,
  };
  try {
    const data = await apiPost('/api/set/' + encodeURIComponent(S.set.id) + '/review', payload);
    const rec = data.record || {};
    S.statuses[key] = {
      s: rec.status || '', t: rec.tags || [],
      n: rec.note ? 1 : 0, c: (rec.annotations || []).length,
      x: 0,   // 刚按当前源文件审过，不再是过期结果
    };
    S.lastNote = S.draft.note;
    reconcileStats(data.stats);
    renderStats();
    if (S.view === 'wall') renderWall();
  } catch (err) {
    toast('保存失败：' + err.message, { error: true, duration: 6000 });
  }
}

function reconcileStats(stats) {
  if (stats) S.stats = Object.assign({}, S.stats, stats);
}

function scheduleNoteSave() {
  clearTimeout(S.noteTimer);
  S.noteTimer = setTimeout(flush, 400);
}

function setStatus(status, options) {
  if (!S.cur || !S.draft) return;
  const key = S.cur.key;
  const was = S.draft.status;
  const next = (S.draft.status === status) ? '' : status;
  S.draft.status = next;
  paintDraftControls();
  const shouldAdvance = !(options && options.noAdvance) &&
    S.autoAdvance && next !== '' && next !== was;
  flush().then(() => {
    // 只有在用户没有翻走、且确实标记成功时才自动前进。
    // F 落章：判定落章是产品里最有仪式感的一刻，但若立刻跳卡，wax-in 0.45s 动画
    // 没播完视图就切走（落章实际不可见）。但完整等 animationend 再跳，在批量审批
    // 场景是真实效率负担（评审实测 1.10s/张 ≈ 100 张 2 分钟纯等待）。取并行节奏：
    // 落章 0.5s 里「落下(55%)+压下接触(72%)」在 ~0.36s 完成，420ms 跳卡让压下帧被看见、
    // 新卡在章回弹定型的尾段抽上来——「章快落定、新卡已抽」，手感与效率兼顾。
    if (shouldAdvance && S.cur && S.cur.key === key && S.draft && S.draft.status === next) {
      const advance = () => { if (S.cur && S.cur.key === key && S.draft && S.draft.status === next) step(1); };
      const wax = document.querySelector('#stageWax.oncard.enter, #statBadge.wax.enter, #textStatBadge.wax.enter');
      if (wax) setTimeout(advance, 420);   // 压下帧播完后、回弹尾段跳卡
      else advance();                       // 无章（清除标记等）立即跳
    }
  });
}

function toggleTag(tag) {
  if (!S.cur || !S.draft) return;
  const i = S.draft.tags.indexOf(tag);
  if (i >= 0) S.draft.tags.splice(i, 1); else S.draft.tags.push(tag);
  paintDraftControls();
  flush();
}

function step(delta) {
  if (!S.filtered.length) return;
  const next = clamp(S.cursor + delta, 0, S.filtered.length - 1);
  if (next === S.cursor && delta !== 0) { toast('已经是' + (delta > 0 ? '最后' : '第一') + '一张了'); return; }
  S.cursor = next;
  S.annotFocus = -1;
  renderDetail();
}

function jumpTo(pos) {
  S.cursor = clamp(pos, 0, S.filtered.length - 1);
  S.annotFocus = -1;
  renderDetail();
}

/* ============================================================
   批量操作
   ============================================================ */

function batchTarget() {
  if (S.selected.size) {
    return { keys: Array.from(S.selected), desc: '已选中的 ' + S.selected.size + ' 条素材' };
  }
  return {
    keys: S.filtered.map((i) => S.items[i].key),
    desc: '当前筛选出的 ' + S.filtered.length + ' 条素材',
  };
}

function openBatchModal(status, mustSelect) {
  if (mustSelect && !S.selected.size) {
    toast('请先点「多选」并选中要操作的素材', { error: true });
    return;
  }
  const target = batchTarget();
  if (!target.keys.length) { toast('没有可操作的素材', { error: true }); return; }
  const label = STATUS_CN[status] || '未审';
  let body;
  if (status === '') {
    // 清除审批结果：文案要说清楚会清空什么、还能不能找回
    $('#batchTitle').textContent = '清除审批结果';
    body = '<div>将把 <b>' + target.keys.length + '</b> 条素材（' + esc(target.desc) + '）还原为「未审」。</div>' +
      '<div class="warn" style="margin-top:8px">这些素材当前的通过/打回/存疑结论、标签、备注和圈选都会被清空。<br>' +
      '原记录仍保留在 history 流水里，确定后 9 秒内可点 toast 上的「撤销」恢复。</div>';
  } else {
    $('#batchTitle').textContent = '批量标记为「' + label + '」';
    body = '<div>将对 <b>' + target.keys.length + '</b> 条素材（' + esc(target.desc) + '）执行标记。</div>' +
      '<div class="warn" style="margin-top:8px">此操作会覆盖这些素材原有的审批状态。</div>';
  }
  if (status === 'reject' || status === 'flag') {
    const templates = S.cfg.templates || [];
    if (templates.length) {
      body += '<div style="margin-top:12px"><label style="color:var(--text-dim);font-size:12px">' +
        '常用模板（点一下自动勾选对应标签）</label>' +
        '<div id="modalTemplates" style="display:flex;gap:6px;flex-wrap:wrap;margin-top:6px">' +
        templates.map((t) => '<span class="tag" data-mtemplate="' + esc(t.id) + '" title="' +
          esc(t.tags.join('、')) + '">' + esc(t.name) + '</span>').join('') +
        '</div></div>';
    }
    body += '<div style="margin-top:12px"><label style="color:var(--text-dim);font-size:12px">统一附加问题标签（可多选）</label>' +
      '<div id="modalTags" style="display:flex;gap:6px;flex-wrap:wrap;margin-top:6px">' +
      S.cfg.tags.map((t) => '<span class="tag" data-mtag="' + esc(t) + '">' + esc(t) + '</span>').join('') +
      '</div></div>';
  }
  $('#batchBody').innerHTML = body;
  $('#batchModal').classList.add('show');
  S.batchPending = { status: status, keys: target.keys };

  $$('#modalTags .tag').forEach((el) => el.addEventListener('click', () => el.classList.toggle('on')));
  // 模板：点一下把它包含的标签全部勾上（再点取消）
  $$('#modalTemplates .tag').forEach((el) => el.addEventListener('click', () => {
    const found = (S.cfg.templates || []).find((t) => t.id === el.dataset.mtemplate);
    if (!found) return;
    const on = !el.classList.contains('on');
    el.classList.toggle('on', on);
    found.tags.forEach((tag) => {
      const chip = document.querySelector('#modalTags .tag[data-mtag="' + tag + '"]');
      if (chip) chip.classList.toggle('on', on);
    });
  }));
}

async function runBatch() {
  const pending = S.batchPending;
  if (!pending) return;
  const tags = $$('#modalTags .tag.on').map((el) => el.dataset.mtag);
  $('#batchModal').classList.remove('show');
  busy(true);
  try {
    const data = await apiPost('/api/set/' + encodeURIComponent(S.set.id) + '/batch',
      { keys: pending.keys, status: pending.status, tags: tags.length ? tags : undefined });
    const keys = pending.keys;
    keys.forEach((k) => {
      const rec = S.statuses[k] || { s: '', t: [], n: 0, c: 0, x: 0 };
      rec.s = pending.status;
      rec.x = 0;
      if (tags.length) {
        rec.t = (rec.t || []).filter((t) => tags.indexOf(t) < 0).concat(tags);
      }
      // 批量清除审批（status === ''）时，本地缓存的标签/圈选数也要清零，
      // 不然缩略图角标和详情页的圈选列表会残留旧数据
      if (pending.status === '') {
        rec.t = [];
        rec.n = 0;
        rec.c = 0;
      }
      S.statuses[k] = rec;
    });
    S.selected.clear();
    if (S.selectMode) {
      // 批量后把多选模式也收掉，避免"已清除的框还亮着"的错觉
      S.selectMode = false;
      $('#wallSelectMode').classList.remove('on');
    }
    // 当前正在详情的这条如果在批量清除范围内，把 draft 一起清空，
    // 不然画布上的圈选框和侧栏的圈选列表会残留
    if (pending.status === '' && S.cur && keys.indexOf(S.cur.key) >= 0 && S.draft) {
      S.draft = { status: '', tags: [], note: '', annotations: [] };
      S.annotFocus = -1;
      S.lastNote = '';
      paintDraftControls();
      renderAnnotList();
      drawAnnotations();
    }
    // 批量改动会让服务端的记录变化，但 getItem 走的是 itemCache 缓存。
    // 把受影响 key 的缓存清掉，下次 renderDetail 才会拿到服务端最新数据，
    // 不然已清除的圈选/标签会从缓存里"复活"
    keys.forEach((k) => S.itemCache.delete(k));
    renderAll();
    toast('已把 ' + data.changed + ' 条标记为「' + (STATUS_CN[pending.status] || '未审') + '」', {
      duration: 9000, actionLabel: '撤销',
      action: () => undoKeys(keys),
    });
  } catch (err) {
    toast('批量操作失败：' + err.message, { error: true, duration: 7000 });
  } finally {
    busy(false);
  }
}

async function undoKeys(keys) {
  busy(true);
  let done = 0;
  try {
    for (const key of keys) {
      try {
        const data = await apiPost('/api/set/' + encodeURIComponent(S.set.id) + '/undo', { key: key });
        const rec = data.record || {};
        S.statuses[key] = { s: rec.status || '', t: rec.tags || [], n: rec.note ? 1 : 0, c: (rec.annotations || []).length };
        done++;
      } catch (err) { /* 该条没有可撤销的历史，跳过 */ }
    }
    renderAll();
    if (S.cur) {
      const rec = recordOf(S.cur.key);
      S.draft.status = rec.s || '';
      S.draft.tags = (rec.t || []).slice();
      paintDraftControls();
    }
    toast('已撤销 ' + done + ' 条');
  } finally {
    busy(false);
  }
}

/** 把当前这条的结论清掉（回到未审），用于「源文件变了、得重新看一遍」。 */
async function clearCurrentResult() {
  if (!S.cur) return;
  if (!window.confirm('清除这一条的审批结论，让它回到未审？\n（原记录仍在 history 流水里）')) return;
  busy(true);
  try {
    const data = await apiPost('/api/set/' + encodeURIComponent(S.set.id) + '/review',
      { key: S.cur.key, status: '', tags: [], note: '', annotations: [] });
    const rec = data.record || {};
    S.statuses[S.cur.key] = { s: rec.status || '', t: [], n: 0, c: 0, x: 0 };
    S.draft = { status: '', tags: [], note: '', annotations: [] };
    S.lastNote = '';
    paintDraftControls();
    renderStats();
    renderAnnotList();
    drawAnnotations();
    toast('已清除这条的结论，可以重新审了');
  } catch (err) {
    toast('清除失败：' + err.message, { error: true });
  } finally { busy(false); }
}

async function undoCurrent() {
  if (!S.cur) return;
  try {
    const data = await apiPost('/api/set/' + encodeURIComponent(S.set.id) + '/undo', { key: S.cur.key });
    const rec = data.record || {};
    S.statuses[S.cur.key] = { s: rec.status || '', t: rec.tags || [], n: rec.note ? 1 : 0, c: (rec.annotations || []).length };
    S.draft.status = rec.status || '';
    S.draft.tags = (rec.tags || []).slice();
    S.draft.note = rec.note || '';
    S.draft.annotations = rec.annotations || [];
    paintDraftControls();
    renderStats();
    renderAnnotList();
    drawAnnotations();
    toast('已撤销上一步');
  } catch (err) {
    toast('没有可撤销的上一步', { error: true });
  }
}

/* ============================================================
   事件绑定
   ============================================================ */

function bindUI() {
  // 素材集切换
  $('#setSelect').addEventListener('change', (e) => loadSet(e.target.value));

  // 视图切换
  $$('#viewSeg button').forEach((b) => b.addEventListener('click', () => setView(b.dataset.view)));

  // 状态按钮
  $$('#statusBtns button').forEach((b) => b.addEventListener('click', () => setStatus(b.dataset.status)));

  // 标签
  $('#tagChips').addEventListener('click', (e) => {
    const el = e.target.closest('.tag');
    if (el) toggleTag(el.dataset.tag);
  });

  // 备注
  $('#noteInput').addEventListener('input', (e) => {
    if (!S.draft) return;
    S.draft.note = e.target.value;
    scheduleNoteSave();
  });

  // 上一个 / 下一个
  $('#btnPrev').addEventListener('click', () => step(-1));
  $('#btnNext').addEventListener('click', () => step(1));

  // 缩放控制
  $('#btnFit').addEventListener('click', fitNow);
  $('#btnZoom100').addEventListener('click', () => {
    S.zoom.scale = 1; S.zoom.tx = 0; S.zoom.ty = 0; S.zoom.userAdjusted = true; applyTransform();
  });
  $('#btnZoomIn').addEventListener('click', () => {
    const r = activeViewport().getBoundingClientRect();
    zoomAt(r.left + r.width / 2, r.top + r.height / 2, 1.25);
  });
  $('#btnZoomOut').addEventListener('click', () => {
    const r = activeViewport().getBoundingClientRect();
    zoomAt(r.left + r.width / 2, r.top + r.height / 2, 1 / 1.25);
  });
  $('#btnSync').addEventListener('click', () => {
    S.zoom.sync = !S.zoom.sync;
    $('#btnSync').classList.toggle('on', S.zoom.sync);
    toast(S.zoom.sync ? '左右已同步' : '左右已解除同步（仍共用同一视图）');
  });
  $('#btnAnnotate').addEventListener('click', toggleAnnotate);

  // 详情页里的「清除结论，重新审」
  $('#itemMeta').addEventListener('click', (e) => {
    if (e.target.closest('#btnReReview')) clearCurrentResult();
  });

  // 主题切换
  $('#btnTheme').addEventListener('click', cycleTheme);

  // 侧栏抽屉（默认收起，Tab 或这个按钮展开）
  $('#btnSidePanel').addEventListener('click', () => toggleSidePanel());
  $('#btnSideClose').addEventListener('click', () => toggleSidePanel(false));
  toggleSidePanel(false);

  // 模型 3D 控制
  $('#btnModelSync').addEventListener('click', () => {
    if (!window.ModelPreview) return;
    const on = !document.getElementById('btnModelSync').classList.contains('on');
    window.ModelPreview.setModelSync(on);
    document.getElementById('btnModelSync').classList.toggle('on', on);
  });
  $('#btnModelReset').addEventListener('click', () => {
    if (window.ModelPreview) window.ModelPreview.resetModelView();
  });

  // 叠加对比
  $('#btnOverlay').addEventListener('click', () => setOverlayMode(!S.overlay.on));
  $('#btnOverlayOff').addEventListener('click', () => setOverlayMode(false));
  // 两种模式切换：滑块对比 / 半透明叠加
  $$('#overlayModeSeg button').forEach((btn) => {
    btn.addEventListener('click', () => {
      S.overlay.mode = btn.dataset.mode;
      applyOverlay();
    });
  });
  // 半透明模式的透明度滑块
  $('#blendAlpha').addEventListener('input', (e) => {
    S.overlay.alpha = clamp(Number(e.target.value) / 100, 0, 1);
    applyOverlay();
  });
  bindOverlayViewport();

  // 过期结果提示条
  $('#staleShow').addEventListener('click', () => {
    S.filters.special = new Set(['stale']);
    applyFilters();
    renderAll();
    toast('已筛出源文件被改过的 ' + S.filtered.length + ' 条', { duration: 4000 });
  });
  $('#staleClear').addEventListener('click', async () => {
    const count = S.stats.stale || 0;
    if (!count) return;
    if (!window.confirm('把 ' + count + ' 条「源文件已变」的审批结果清掉，让它们回到未审？\n'
      + '（原结果仍留在 history 流水里，不会真的丢）')) return;
    busy(true);
    try {
      const data = await apiPost('/api/set/' + encodeURIComponent(S.set.id) + '/clear-stale', {});
      await loadSet(S.set.id);
      toast('已清掉 ' + data.removed + ' 条过期结果，可以重新审了');
    } catch (err) {
      toast('清理失败：' + err.message, { error: true });
    } finally { busy(false); }
  });
  $('#staleDismiss').addEventListener('click', () => {
    S.staleDismissed = true;
    renderStaleBar();
  });

  // 派生新集合
  $('#btnDerive').addEventListener('click', () => {
    $('#deriveId').value = S.set.id + '_v2';
    $('#deriveName').value = S.set.name + ' · 新一批';
    $('#deriveBRoot').value = '';
    $('#deriveCount').textContent = S.filtered.length;
    $('#deriveModal').classList.add('show');
  });
  $('#deriveCancel').addEventListener('click', () => $('#deriveModal').classList.remove('show'));
  $('#deriveConfirm').addEventListener('click', async () => {
    const newId = $('#deriveId').value.trim();
    if (!/^[A-Za-z0-9_.-]+$/.test(newId)) {
      toast('集合 id 只能用英文、数字、下划线、点、横线', { error: true });
      return;
    }
    const scope = (document.querySelector('input[name=deriveScope]:checked') || {}).value || 'filtered';
    const body = {
      from_set: S.set.id,
      new_id: newId,
      name: $('#deriveName').value.trim() || undefined,
      b_root: $('#deriveBRoot').value.trim() || undefined,
    };
    if (scope === 'filtered') {
      if (!S.filtered.length) { toast('当前筛选结果为空', { error: true }); return; }
      body.only_ids = S.filtered.map((i) => S.items[i].id);
    }
    busy(true);
    try {
      const data = await apiPost('/api/agent/derive-set', body);
      if (data.error) throw new Error(data.error);
      $('#deriveModal').classList.remove('show');
      S.cfg = await apiGet('/api/config');
      fillSetSelect();
      await loadSet(newId);
      S.configHash = (await apiGet('/api/ping')).configHash;   // 自己改的不算"外部变更"
      $('#configBar').classList.remove('show');
      toast('已创建集合「' + newId + '」，共 ' + data.count + ' 条；配置写在 sets.d/ 下', { duration: 6000 });
    } catch (err) {
      toast('创建失败：' + err.message, { error: true, duration: 6000 });
    } finally { busy(false); }
  });

  // 导出圈选裁剪
  $('#btnExportCrops').addEventListener('click', async () => {
    if (S.set.type !== 'image') { toast('文本素材集没有圈选裁剪', { error: true }); return; }
    busy(true);
    try {
      const data = await apiPost('/api/agent/export-crops',
        { set: S.set.id, only: 'problem', download: true });
      if (data.error) throw new Error(data.error);
      if (!data.count) { toast('没有可导出的圈选（这条素材集里还没有圈注）'); return; }
      window.location.href = '/api/download?path=' + encodeURIComponent(data.zip);
      toast('已导出 ' + data.count + ' 处圈选裁剪');
    } catch (err) {
      toast('导出失败：' + err.message, { error: true });
    } finally { busy(false); }
  });

  // 配置更新提示
  $('#configReload').addEventListener('click', () => location.reload());
  $('#configLater').addEventListener('click', () => {
    S.configHash = null;          // 下次探测再提示
    $('#configBar').classList.remove('show');
  });

  // 轮次
  $('#btnPrevRound').addEventListener('click', async () => {
    busy(true);
    try {
      const data = await apiGet('/api/set/' + encodeURIComponent(S.set.id) + '/previous-round');
      if (!data.ids || !data.ids.length) {
        toast(data.round ? ('第 ' + data.round + ' 轮没有打回的素材') : '还没有归档的上一轮结果', { duration: 4000 });
        return;
      }
      S.filters.prevIds = new Set(data.ids);
      applyFilters();
      renderAll();
      toast('已筛出第 ' + data.round + ' 轮打回的 ' + data.ids.length + ' 条，优先复审它们', { duration: 5000 });
    } catch (err) {
      toast('读取上轮结果失败：' + err.message, { error: true });
    } finally { busy(false); }
  });
  $('#btnNewRound').addEventListener('click', async () => {
    const reviewed = (S.stats.reviewed || 0);
    if (!window.confirm('归档本轮结果并开始新一轮？\n\n'
      + '· 本轮 ' + reviewed + ' 条审批结果会存成单独文件（可随时找回）\n'
      + '· 当前批次清空，全部回到未审\n'
      + '· 该素材集的缩略图缓存一起清掉')) return;
    busy(true);
    try {
      const data = await apiPost('/api/set/' + encodeURIComponent(S.set.id) + '/new-round', {});
      await loadSet(S.set.id);
      toast('第 ' + data.previousRound + ' 轮已归档到 ' +
        String(data.archivedJson || '').split(/[\\/]/).pop() + '，现在是第 ' + data.round + ' 轮',
        { duration: 6000 });
    } catch (err) {
      toast('归档失败：' + err.message, { error: true });
    } finally { busy(false); }
  });

  // 文本控制
  $('#btnFontUp').addEventListener('click', () => { S.textFont = clamp(S.textFont + 2, 10, 34); applyTextStyle(); });
  $('#btnFontDown').addEventListener('click', () => { S.textFont = clamp(S.textFont - 2, 10, 34); applyTextStyle(); });
  $('#btnWrap').addEventListener('click', () => {
    S.textWrap = !S.textWrap; $('#btnWrap').classList.toggle('on', S.textWrap); applyTextStyle();
  });
  $('#btnTextSync').addEventListener('click', () => {
    S.textSync = !S.textSync; $('#btnTextSync').classList.toggle('on', S.textSync);
  });
  $('#btnLayout').addEventListener('click', () => {
    S.textStacked = !S.textStacked;
    $('#btnLayout').textContent = S.textStacked ? '左右分栏' : '上下分栏';
    applyTextStyle();
  });

  // 文本同步滚动
  ['#textA', '#textB'].forEach((sel, index) => {
    $(sel).addEventListener('scroll', () => {
      if (!S.textSync) return;
      const other = $(index === 0 ? '#textB' : '#textA');
      const self = $(sel);
      const selfMax = self.scrollHeight - self.clientHeight;
      const otherMax = other.scrollHeight - other.clientHeight;
      if (selfMax <= 0 || otherMax <= 0) return;
      other.scrollTop = (self.scrollTop / selfMax) * otherMax;
    });
  });

  // 画布交互
  ['a', 'b'].forEach((side) => bindViewport(side));

  // 圈选列表交互
  $('#annotList').addEventListener('click', (e) => {
    const del = e.target.closest('[data-annot-del]');
    if (del) { deleteAnnotation(Number(del.dataset.annotDel)); return; }
    const item = e.target.closest('li[data-annot]');
    if (item && !e.target.closest('input')) focusAnnotation(Number(item.dataset.annot));
  });
  $('#annotList').addEventListener('input', (e) => {
    const input = e.target.closest('[data-annot-note]');
    if (!input) return;
    const i = Number(input.dataset.annotNote);
    if (S.draft && S.draft.annotations[i]) {
      S.draft.annotations[i].note = input.value;
      scheduleNoteSave();
    }
  });
  // 圈选后自动展开的侧栏，输入完（回车或点走）自动收起。
  // 用捕获阶段 blur 处理「点走」；回车在 onKeyDown 的 typing 分支里处理。
  document.addEventListener('blur', (e) => {
    if (!e.target || !e.target.classList || !e.target.classList.contains('annot-note')) return;
    if (S.annotAutoOpened) {
      S.annotAutoOpened = false;
      toggleSidePanel(false);
    }
  }, true);
  $('#overlayA').addEventListener('click', (e) => {
    const el = e.target.closest('[data-annot]');
    if (el) focusAnnotation(Number(el.dataset.annot));
  });
  $('#overlayB').addEventListener('click', (e) => {
    const el = e.target.closest('[data-annot]');
    if (el) focusAnnotation(Number(el.dataset.annot));
  });

  // 批量
  $('#batchPass').addEventListener('click', () => openBatchModal('pass'));
  $('#batchFlag').addEventListener('click', () => openBatchModal('flag'));
  $('#batchReject').addEventListener('click', () => openBatchModal('reject'));
  $('#batchCancel').addEventListener('click', () => $('#batchModal').classList.remove('show'));
  $('#batchConfirm').addEventListener('click', runBatch);

  // 导出
  $$('[data-export]').forEach((btn) => btn.addEventListener('click', () => {
    const url = '/api/set/' + encodeURIComponent(S.set.id) + '/export?format=' +
      btn.dataset.export + '&only=' + btn.dataset.only +
      (btn.dataset.part ? '&part=' + btn.dataset.part : '');
    window.location.href = url;
  }));

  // 搜索
  let searchTimer = null;
  $('#searchBox').addEventListener('input', (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      S.filters.search = e.target.value;
      applyFilters();
      renderAll();
    }, 220);
  });

  // 快捷状态筛选
  $('#quickFilters').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-status]');
    if (!btn) return;
    S.filters.status = btn.dataset.status;
    applyFilters();
    renderAll();
  });

  // 筛选面板
  $('#btnFilter').addEventListener('click', () => {
    $('#fStatus').querySelectorAll('button').forEach((b) =>
      b.classList.toggle('on', b.dataset.status === S.filters.status));
    $$('#fTags .tag').forEach((el) => el.classList.toggle('on', S.filters.tags.has(el.dataset.ftag)));
    $$('#fSpecial .tag').forEach((el) =>
      el.classList.toggle('on', (S.filters.special || new Set()).has(el.dataset.special)));
    $('#fSearch').value = S.filters.search || '';
    renderFacetInputs();
    $('#filterModal').classList.add('show');
  });
  $('#fStatus').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-status]');
    if (!btn) return;
    $('#fStatus').querySelectorAll('button').forEach((b) => b.classList.remove('on'));
    btn.classList.add('on');
  });
  $('#fTags').addEventListener('click', (e) => {
    const el = e.target.closest('.tag');
    if (el) el.classList.toggle('on');
  });
  $('#fSpecial').addEventListener('click', (e) => {
    const el = e.target.closest('.tag');
    if (el) el.classList.toggle('on');
  });
  $('#fApply').addEventListener('click', () => {
    const statusBtn = $('#fStatus button.on');
    S.filters.status = statusBtn ? statusBtn.dataset.status : 'all';
    S.filters.tags = new Set($$('#fTags .tag.on').map((el) => el.dataset.ftag));
    S.filters.special = new Set($$('#fSpecial .tag.on').map((el) => el.dataset.special));
    const facets = {};
    $$('#fFacets select[data-facet]').forEach((sel) => { if (sel.value) facets[sel.dataset.facet] = sel.value; });
    S.filters.facets = facets;
    S.filters.search = $('#fSearch').value;
    $('#searchBox').value = S.filters.search;
    $('#filterModal').classList.remove('show');
    applyFilters();
    renderAll();
  });
  $('#fReset').addEventListener('click', () => {
    $('#filterModal').classList.remove('show');
    resetFilters();
  });

  // 帮助 / 设置
  $('#btnHelp').addEventListener('click', () => $('#helpModal').classList.add('show'));
  $('#helpClose').addEventListener('click', () => $('#helpModal').classList.remove('show'));
  $('#btnSettings').addEventListener('click', () => $('#settingsModal').classList.add('show'));
  $('#settingsClose').addEventListener('click', () => $('#settingsModal').classList.remove('show'));
  $('#btnReload').addEventListener('click', async () => {
    busy(true);
    try {
      await apiPost('/api/reload');
      S.cfg = await apiGet('/api/config');
      $('#settingsModal').classList.remove('show');
      await loadSet(S.set ? S.set.id : S.cfg.sets[0].id);
      toast('配置已重新加载');
    } catch (err) {
      toast('重新加载失败：' + err.message, { error: true });
    } finally { busy(false); }
  });
  $('#btnClearThumbs').addEventListener('click', async () => {
    busy(true);
    try {
      const data = await apiPost('/api/clear-thumb-cache');
      toast('已清空缩略图缓存（' + data.removed + ' 个文件）');
    } catch (err) {
      toast('清空失败：' + err.message, { error: true });
    } finally { busy(false); }
  });
  $$('.modal-mask').forEach((mask) => mask.addEventListener('click', (e) => {
    if (e.target === mask) mask.classList.remove('show');
  }));

  // 自动跳下一张
  const auto = $('#autoAdv');
  if (auto) {
    auto.checked = S.autoAdvance;
    auto.addEventListener('change', () => { S.autoAdvance = auto.checked; });
  }

  // 墙工具栏
  $('#wallShowA').addEventListener('click', () => { S.wallSide = 'a'; updateWallSideButtons(); renderWall(); });
  $('#wallShowB').addEventListener('click', () => { S.wallSide = 'b'; updateWallSideButtons(); renderWall(); });
  $('#wallSelectMode').addEventListener('click', () => {
    S.selectMode = !S.selectMode;
    $('#wallSelectMode').classList.toggle('on', S.selectMode);
    if (!S.selectMode) { S.selected.clear(); }
    renderWall();
  });
  $('#wallSelectAll').addEventListener('click', () => {
    const pageSize = S.cfg.wallPageSize || 60;
    const start = (S.wallPage - 1) * pageSize;
    S.filtered.slice(start, start + pageSize).forEach((i) => S.selected.add(S.items[i].key));
    renderWall();
  });
  $('#wallSelectNone').addEventListener('click', () => { S.selected.clear(); renderWall(); });
  $('#wallBatchPass').addEventListener('click', () => openBatchModal('pass'));
  $('#wallBatchFlag').addEventListener('click', () => openBatchModal('flag'));
  $('#wallBatchReject').addEventListener('click', () => openBatchModal('reject'));
  $('#wallClearCurrent').addEventListener('click', () => openBatchModal('', true));
  $('#wallSizeUp').addEventListener('click', () => { S.wallCell = clamp(S.wallCell + 40, 100, 400); applyWallCell(); });
  $('#wallSizeDown').addEventListener('click', () => { S.wallCell = clamp(S.wallCell - 40, 100, 400); applyWallCell(); });
  $('#wallDim').addEventListener('click', () => {
    S.wallDim = !S.wallDim;
    $('#wallDim').classList.toggle('on', S.wallDim);
    renderWall();
  });
  $('#wallGrid').addEventListener('click', onWallClick);

  // 分页
  $('#pageFirst').addEventListener('click', () => { S.wallPage = 1; renderWall(); });
  $('#pagePrev').addEventListener('click', () => { S.wallPage--; renderWall(); });
  $('#pageNext').addEventListener('click', () => { S.wallPage++; renderWall(); });
  $('#pageLast').addEventListener('click', () => {
    const pageSize = S.cfg.wallPageSize || 60;
    S.wallPage = Math.max(1, Math.ceil(S.filtered.length / pageSize));
    renderWall();
  });
  $('#pageJump').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    const n = parseInt(e.target.value, 10);
    if (!isNaN(n)) { S.wallPage = n; renderWall(); }
    e.target.value = '';
  });

  // 键盘
  // 捕获阶段处理：输入框里的按键先被 typing 拦住，不会被冒泡的快捷键判断抢走
  window.addEventListener('keydown', onKeyDown, true);
  window.addEventListener('keyup', onKeyUp);

  // 画布/窗口尺寸变化时，只要用户没手动缩放过，就重新适应窗口
  if (window.ResizeObserver) {
    new ResizeObserver(() => {
      if (S.set && S.set.type === 'image' && S.view === 'detail') tryFit();
    }).observe($('#panes'));
  }
  window.addEventListener('resize', () => {
    if (S.set && S.set.type === 'image') tryFit();
  });
}

function updateWallSideButtons() {
  $('#wallShowA').classList.toggle('on', S.wallSide === 'a');
  $('#wallShowB').classList.toggle('on', S.wallSide === 'b');
}

function applyWallCell() {
  $('#wallGrid').style.setProperty('--cell', S.wallCell + 'px');
  renderWall();
}

function onWallClick(e) {
  const quickAnnot = e.target.closest('[data-quick-annot]');
  if (quickAnnot) {
    e.stopPropagation();
    const pos = Number(quickAnnot.dataset.quickAnnot);
    jumpTo(pos);
    setView('detail');
    S.backToWallAfterAnnotate = true;
    if (!S.annotating) toggleAnnotate();
    toast('已进入圈注模式：在图上拖拽画框，按 Esc 或再按 ' +
      ((S.cfg.shortcuts.annotate || ['c'])[0]) + ' 退出并回到墙上', { duration: 5000 });
    return;
  }
  const cell = e.target.closest('.cell');
  if (!cell) return;
  const pos = Number(cell.dataset.pos);
  const item = S.items[S.filtered[pos]];
  if (!item) return;
  if (S.selectMode) {
    if (S.selected.has(item.key)) S.selected.delete(item.key); else S.selected.add(item.key);
    renderWall();
    return;
  }
  S.backToWallAfterAnnotate = false;
  jumpTo(pos);
  gotoWallPageFor(pos);
  setView('detail');
  S.gotoWallScroll = true;
}

/** 侧栏抽屉：单张对照视图下默认收起，Tab 键或命令栏按钮展开。 */
function toggleSidePanel(force) {
  const panel = $('#sidePanel');
  const open = force !== undefined ? force : !panel.classList.contains('open');
  panel.classList.toggle('open', open);
  const btn = $('#btnSidePanel');
  if (btn) btn.classList.toggle('on', open);
  // 抽屉改变布局后重新适应画布
  if (S.set && S.set.type === 'image') tryFit();
}

function toggleAnnotate() {
  if (S.set.type !== 'image') return;
  if (!S.annotating && S.overlay.on) setOverlayMode(false);   // 叠加模式下先退出叠加
  S.annotating = !S.annotating;
  $('#btnAnnotate').classList.toggle('on', S.annotating);
  $$('.viewport').forEach((vp) => vp.classList.toggle('annotating', S.annotating));
  if (S.annotating) {
    toast('圈图模式：在图上拖拽画框（再按一次或 Esc 退出）');
  } else {
    toast('已退出圈图模式');
    if (S.backToWallAfterAnnotate) {
      S.backToWallAfterAnnotate = false;
      S.gotoWallScroll = true;
      gotoWallPageFor(S.cursor);
      setView('wall');
      return;
    }
  }
}

/* ------------------------------------------------------------ 画布事件 */

function bindViewport(side) {
  const vp = viewportFor(side);
  let panning = false, drawStart = null, startX = 0, startY = 0, moved = false;

  vp.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    // 点在已有圈选框上：交给 click 事件做定位，不要开始拖拽
    if (!S.annotating && e.target.closest('.annot')) return;
    if (S.annotating) {
      const p = stageToNormalized(side, e.clientX, e.clientY);
      if (!p) return;
      drawStart = p;
      startX = e.clientX; startY = e.clientY; moved = false;
      e.preventDefault();
      return;
    }
    panning = true; moved = false;
    startX = e.clientX; startY = e.clientY;
    vp.classList.add('dragging');
    e.preventDefault();
  });

  window.addEventListener('mousemove', (e) => {
    if (drawStart) {
      const p = stageToNormalized(side, e.clientX, e.clientY);
      if (!p) return;
      if (Math.abs(e.clientX - startX) > 3 || Math.abs(e.clientY - startY) > 3) moved = true;
      drawDraftRect(side, drawStart, p);
      return;
    }
    if (!panning) return;
    const dx = e.clientX - startX, dy = e.clientY - startY;
    if (Math.abs(dx) > 2 || Math.abs(dy) > 2) moved = true;
    S.zoom.tx += dx; S.zoom.ty += dy;
    S.zoom.userAdjusted = true;
    startX = e.clientX; startY = e.clientY;
    applyTransform();
  });

  window.addEventListener('mouseup', (e) => {
    if (drawStart) {
      const p = stageToNormalized(side, e.clientX, e.clientY);
      clearDraftRect();
      if (p && moved) {
        const box = {
          x: Math.min(drawStart.x, p.x), y: Math.min(drawStart.y, p.y),
          w: Math.abs(p.x - drawStart.x), h: Math.abs(p.y - drawStart.y),
        };
        if (box.w > 0.008 && box.h > 0.008) addAnnotation(side, box);
      }
      drawStart = null;
      return;
    }
    if (panning) { panning = false; vp.classList.remove('dragging'); }
  });

  vp.addEventListener('wheel', (e) => {
    e.preventDefault();
    zoomAt(e.clientX, e.clientY, e.deltaY < 0 ? 1.15 : 1 / 1.15);
  }, { passive: false });

  vp.addEventListener('dblclick', () => {
    if (Math.abs(S.zoom.scale - S.zoom.fit) < 0.001) {
      S.zoom.scale = 1; S.zoom.tx = 0; S.zoom.ty = 0; S.zoom.userAdjusted = true;
    } else {
      S.zoom.userAdjusted = false;
      fitNow();
      return;
    }
    applyTransform();
  });
}

function drawDraftRect(side, a, b) {
  const overlay = $('#overlay' + side.toUpperCase());
  let draft = overlay.querySelector('.annot-draft');
  if (!draft) {
    draft = document.createElement('div');
    draft.className = 'annot-draft';
    overlay.appendChild(draft);
  }
  draft.style.left = (Math.min(a.x, b.x) * 100) + '%';
  draft.style.top = (Math.min(a.y, b.y) * 100) + '%';
  draft.style.width = (Math.abs(b.x - a.x) * 100) + '%';
  draft.style.height = (Math.abs(b.y - a.y) * 100) + '%';
}

function clearDraftRect() {
  $$('.annot-draft').forEach((el) => el.remove());
}

/* ------------------------------------------------------------ 键盘 */

function onKeyDown(e) {
  // 焦点在输入框里时，所有字母/数字键都交给输入框，不触发工具快捷键。
  // 用 e.target 兜底：真实打字时 target 就是 input；合成事件 target 是 window 时
  // 再读 document.activeElement（dispatchEvent 可能让 activeElement 在传播中被改掉）。
  const tag = (e.target.tagName || '').toLowerCase();
  const active = document.activeElement;
  const activeIsTextInput = !!(active && (
    active.tagName === 'INPUT' || active.tagName === 'TEXTAREA'
    || active.tagName === 'SELECT' || active.isContentEditable
  ));
  const typing = tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable
    || activeIsTextInput;

  // 按住空格：叠加模式下临时看全幅原图
  if (e.key === ' ' || e.code === 'Space') {
    if (!typing && S.overlay.on) {
      e.preventDefault();
      blinkOverlay(true);
      return;
    }
  }

  if (e.key === 'Escape') {
    if (S.annotating) { toggleAnnotate(); return; }
    $$('.modal-mask.show').forEach((m) => m.classList.remove('show'));
    if (typing) e.target.blur();
    return;
  }
  if (typing) {
    if (e.key === 'Enter' && (e.target.id === 'noteInput' || e.target.classList.contains('annot-note'))) {
      e.target.blur();
      // 圈选后自动展开的侧栏，按回车直接收起（不用等 blur 冒泡）
      if (e.target.classList.contains('annot-note') && S.annotAutoOpened) {
        S.annotAutoOpened = false;
        toggleSidePanel(false);
      }
    }
    return;
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') {
    e.preventDefault(); undoCurrent(); return;
  }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if ($$('.modal-mask.show').length) return;
  if (e.repeat) return;   // 按住不放不要连续标记

  const map = S.cfg.shortcuts || {};
  const hit = (action) => (map[action] || []).some((k) => e.key === k || e.key.toLowerCase() === String(k).toLowerCase());

  if (hit('next')) { e.preventDefault(); step(1); return; }
  if (hit('prev')) { e.preventDefault(); step(-1); return; }
  if (hit('pass')) { e.preventDefault(); setStatus('pass'); return; }
  if (hit('reject')) { e.preventDefault(); setStatus('reject'); return; }
  if (hit('flag')) { e.preventDefault(); setStatus('flag'); return; }
  if (hit('clear')) { e.preventDefault(); setStatus(''); return; }
  if (hit('wall')) { e.preventDefault(); setView('wall'); return; }
  if (hit('detail')) { e.preventDefault(); setView('detail'); return; }
  if (hit('annotate')) { e.preventDefault(); toggleAnnotate(); return; }
  if (hit('overlay')) { e.preventDefault(); setOverlayMode(!S.overlay.on); return; }
  if (hit('fit')) { e.preventDefault(); fitNow(); return; }
  if (hit('zoom100')) {
    e.preventDefault();
    S.zoom.scale = 1; S.zoom.tx = 0; S.zoom.ty = 0; S.zoom.userAdjusted = true;
    applyTransform(); return;
  }
  if (hit('sync')) { e.preventDefault(); $('#btnSync').click(); return; }
  if (hit('focusSearch')) { e.preventDefault(); $('#searchBox').focus(); $('#searchBox').select(); return; }
  if (e.key === 'Tab' && !typing && !e.shiftKey && S.view === 'detail') {
    e.preventDefault();
    toggleSidePanel();
    return;
  }

  // 问题标签快捷键：按一下就等于「打回 + 贴这个标签」，全键盘完成一次打回
  const tagKeys = S.cfg.tagShortcuts || [];
  const tagIndex = tagKeys.findIndex((k) => e.key.toLowerCase() === String(k).toLowerCase());
  if (tagIndex >= 0 && tagIndex < S.cfg.tags.length) {
    e.preventDefault();
    applyTagByHotkey(S.cfg.tags[tagIndex]);
    return;
  }
}

function onKeyUp(e) {
  if (e.key === ' ' || e.code === 'Space') blinkOverlay(false);
}

/** 标签快捷键：贴/取消标签；如果这张还没审过，顺手置成打回（打标签基本就是判它有问题）。
 *  这里不自动跳下一张，因为一张卡可能要点好几个标签。 */
function applyTagByHotkey(tagName) {
  if (!S.cur || !S.draft) return;
  const had = S.draft.tags.indexOf(tagName) >= 0;
  toggleTag(tagName);
  if (!had && !S.draft.status) setStatus('reject', { noAdvance: true });
  toast('标签：' + tagName + (had ? '（已取消）' : ''), { duration: 1400 });
}

/* ------------------------------------------------------------ go */

init();
