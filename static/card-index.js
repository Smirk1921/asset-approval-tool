/* ============================================================
   索引卡盒 Card Index 增强层（F 方向 · 定稿共识整合版）
   在 app.js 之后加载；复用全局 S，不修改 app.js 源码。
   提供：① queueBar 队列条（tab 切边、点击跳卡）
         ② statBadge → 楷体描线小圆章（wax）
   实现说明：app.js 内部自调用不经过 window.*，所以不包装函数，
   改用 MutationObserver 监听 statBadge（判定变化）与 posLabel
   （翻卡/切集/筛选变化），在它们变化后重绘 queueBar 与 wax。
   ============================================================ */
(function () {
  const $q = (s) => document.querySelector(s);

  /* ---------------- 队列条 ---------------- */
  function renderQueueBar() {
    const bar = $q('#queueBar');
    if (!bar || !window.S) return;
    const S = window.S;
    if (S.view !== 'detail' || !S.filtered || !S.filtered.length) {
      bar.style.display = 'none';
      return;
    }
    bar.style.display = 'flex';
    const N = 21, half = Math.floor(N / 2);
    const len = S.filtered.length;
    let start = Math.max(0, Math.min(S.cursor - half, len - N));
    const end = Math.min(len, start + N);
    start = Math.max(0, end - Math.min(N, len));
    const frag = document.createDocumentFragment();
    for (let pos = start; pos < end; pos++) {
      const it = S.items[S.filtered[pos]];
      const rec = (S.statuses && S.statuses[it.key]) || {};
      const st = rec.s || '';
      const tab = document.createElement('button');
      tab.className = 'qtab' + (st ? ' st-' + st : '') + (pos === S.cursor ? ' cur' : '');
      tab.textContent = it.id;
      tab.title = it.name + (st ? ' · ' + ({ pass: '通过', reject: '打回', flag: '存疑' }[st] || st) : ' · 未审');
      tab.addEventListener('click', () => {
        if (pos === S.cursor) return;
        S.cursor = pos;
        if (typeof window.renderDetail === 'function') window.renderDetail();
      });
      frag.appendChild(tab);
    }
    bar.innerHTML = '';
    bar.appendChild(frag);
    const cur = bar.querySelector('.qtab.cur');
    if (cur) cur.scrollIntoView({ block: 'nearest', inline: 'center' });
  }

  /* ---------------- 楷体章 ---------------- */
  function paintWax() {
    if (!window.S || !S.draft) return;
    const st = S.draft.status || '';
    const cn = { pass: '通过', reject: '打回', flag: '存疑' }[st] || '';
    [$q('#statBadge'), $q('#textStatBadge')].forEach((el) => {
      if (!el) return;
      if (!st) {
        if (el.dataset.st) { el.className = 'meta'; el.innerHTML = ''; delete el.dataset.st; }
        return;
      }
      const wasSt = el.dataset.st || '';
      // 章压在 B 卡 pane-head 右端（statBadge 本身就在 B 卡头部 spacer 后）
      el.className = 'meta wax ' + st + (wasSt !== st ? ' enter' : '');
      el.dataset.st = st;
      el.textContent = cn;
      el.title = '状态：' + cn;
      if (wasSt !== st) {
        el.addEventListener('animationend', () => el.classList.remove('enter'), { once: true });
      }
    });
  }

  /* ---------------- 换卡过渡（两拍换卡的第二拍） ----------------
     queueBar / 翻页触发 renderDetail 时，卡内容区是同步 DOM 替换、无过渡。
     F 的「抽卡」语言要求新卡有一个克制的浮出：淡入 + 微升 8px，0.28s。
     posLabel 变化 = 翻卡信号（cursor 变了才换卡，同卡重绘不闪）。 */
  let lastCursor = -1;
  let entered = false;  // 首次进场的出场动画只放一次（等内容真正渲染出来）
  function playEnter() {
    // 两栏分别浮出：左栏（源A）先，右栏（源B/成品）慢 60ms，形成抽卡次序。
    // 各 type 的栏容器：文本=textA/textB，图片=viewportA/B，模型=modelViewportA/B。
    [['#textA', '#viewportA', '#modelViewportA'], ['#textB', '#viewportB', '#modelViewportB']]
      .forEach((sels, side) => {
        sels.forEach((sel) => {
          const host = $q(sel);
          if (!host) return;
          host.classList.remove('card-enter-a', 'card-enter-b');
          void host.offsetWidth;  // 强制 reflow 让动画能重复触发
          host.classList.add(side === 0 ? 'card-enter-a' : 'card-enter-b');
        });
      });
  }
  function cardEnter() {
    if (!window.S) return;
    const cur = S.cursor;
    if (cur === lastCursor) return;
    lastCursor = cur;
    // 只让对照区浮出（panes/textWrap/modelWrap），判定键与 queueBar 保持不动
    playEnter();
  }
  // 首次进场：等对照区第一次出现内容（数据异步加载完成）再放出场动画
  function watchFirstContent() {
    const targets = ['#textWrap', '#panes'].map((s) => $q(s)).filter(Boolean);
    if (!targets.length) { setTimeout(watchFirstContent, 100); return; }
    const obs = new MutationObserver(() => {
      if (entered) { obs.disconnect(); return; }
      const hasContent = targets.some((t) =>
        t.style.display !== 'none' && (t.childElementCount > 0 || (t.textContent || '').trim()));
      if (hasContent) {
        entered = true;
        lastCursor = window.S ? S.cursor : 0;  // 标记已进场，避免 cardEnter 重复
        playEnter();
        obs.disconnect();
      }
    });
    targets.forEach((t) => obs.observe(t, { childList: true, subtree: true, attributes: true }));
  }

  /* ---------------- S1 批量条吸附盒底 ----------------
     多选且有选中时，把散在工具栏的批量操作聚成 #wallBatchDock 贴住卡盒下缘。
     dock 按钮转发到工具栏已绑定的真实 handler，复用 openBatchModal 零风险。 */
  function refreshDock() {
    const dock = $q('#wallBatchDock');
    if (!dock || !window.S) return;
    const n = (S.selected && S.selected.size) || 0;
    const show = S.view === 'wall' && S.selectMode && n > 0;
    dock.classList.toggle('dock', show);
    const cnt = $q('#dockCount');
    if (cnt) cnt.textContent = n;
  }
  function bindDock() {
    const dock = $q('#wallBatchDock');
    if (!dock || dock.dataset.bound) return;
    dock.dataset.bound = '1';
    // 转发到工具栏真实按钮（它们已绑定 openBatchModal）
    const map = { dockPass: 'wallBatchPass', dockFlag: 'wallBatchFlag', dockReject: 'wallBatchReject', dockClear: 'wallClearCurrent' };
    Object.entries(map).forEach(([dockId, realId]) => {
      const btn = $q('#' + dockId);
      if (btn) btn.addEventListener('click', () => { const r = $q('#' + realId); if (r) r.click(); });
    });
    // 选中数变化：app.js 会更新 #wallSelInfo 与 .cell.selected，监听这两处
    const selInfo = $q('#wallSelInfo');
    if (selInfo) new MutationObserver(refreshDock).observe(selInfo, { childList: true, characterData: true, subtree: true });
    const grid = $q('#wallGrid');
    if (grid) new MutationObserver(refreshDock).observe(grid, { attributes: true, attributeFilter: ['class'], subtree: true });
    const sm = $q('#wallSelectMode');
    if (sm) sm.addEventListener('click', () => setTimeout(refreshDock, 0));
  }

  /* ---------------- S3 归档章 ----------------
     归档本轮成功时，app.js 会 toast「第 N 轮已归档到 …」。监听 toast 文本，
     出现「已归档」即在屏中央盖一枚归档圆章（.8s，落下→定格→淡出）。 */
  function bindArchiveSeal() {
    const toast = $q('#toast');
    if (!toast) return;
    let seal = $q('#archiveSeal');
    if (!seal) {
      seal = document.createElement('div');
      seal.id = 'archiveSeal';
      seal.textContent = '归档';
      document.body.appendChild(seal);
    }
    new MutationObserver(() => {
      const txt = (toast.textContent || '');
      if (txt.includes('已归档')) {
        seal.classList.remove('stamp');
        void seal.offsetWidth;
        seal.classList.add('stamp');
      }
    }).observe(toast, { childList: true, characterData: true, subtree: true });
  }

  /* ---------------- 观察变化 ---------------- */
  // statBadge 是 app.js 每次 paintDraftControls 都会写 innerHTML 的元素，
  // 它一变就说明判定/草稿变了 → 重绘章与队列条状态。
  function boot() {
    if (!window.S) { setTimeout(boot, 60); return; }

    let scheduled = false;
    function refresh() {
      if (scheduled) return;
      scheduled = true;
      // 让 app.js 的当次渲染先落地，再重绘
      requestAnimationFrame(() => {
        scheduled = false;
        try { renderQueueBar(); paintWax(); cardEnter(); } catch (e) { /* 不阻断 */ }
      });
    }

    const badge = $q('#statBadge');
    if (badge) {
      new MutationObserver(refresh).observe(badge, { childList: true, characterData: true, subtree: true });
    }
    const pos = $q('#posLabel');
    if (pos) {
      new MutationObserver(refresh).observe(pos, { childList: true, characterData: true, subtree: true });
    }
    const viewSeg = $q('#viewSeg');
    if (viewSeg) {
      viewSeg.addEventListener('click', () => setTimeout(refresh, 0));
    }
    // 判定键点击后 app.js 先写 record 再 paint，refresh 走 rAF 已足够晚
    const statusBtns = $q('#statusBtns');
    if (statusBtns) {
      statusBtns.addEventListener('click', () => setTimeout(refresh, 0));
    }
    const setSel = $q('#setSelect');
    if (setSel) {
      setSel.addEventListener('change', () => setTimeout(refresh, 0));
    }

    watchFirstContent();  // 首次进场的出场动画（等内容渲染出来）
    bindDock();           // S1 批量条吸附盒底
    bindArchiveSeal();    // S3 归档章
    refresh();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
