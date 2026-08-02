/*
 * SciRate snapshot bookmarklet.
 *
 * Runs inside a SciRate page that YOU opened in your own browser, reads the
 * scite counts already rendered on screen, and copies a relay snapshot JSON to
 * the clipboard.  It never fetches anything by itself: every page load is a
 * page you navigated to.  Feed `scirate_snapshot.py` with the clipboard.
 *
 * Target URL shape:
 *   https://scirate.com/arxiv/quant-ph?date=YYYY-MM-DD&range=1   (daily)
 *   https://scirate.com/arxiv/quant-ph?date=YYYY-MM-DD&range=7   (weekly)
 *
 * Multi-page periods: press the bookmarklet on each page in turn.  Rows
 * accumulate in localStorage under the (date, range) key until the lowest
 * scite count on screen drops below the threshold, which is the only point
 * where the snapshot can honestly be called complete.
 */
(function () {
  var MONTHS = {
    Jan: 1, Feb: 2, Mar: 3, Apr: 4, May: 5, Jun: 6,
    Jul: 7, Aug: 8, Sep: 9, Oct: 10, Nov: 11, Dec: 12
  };

  function pad(n) { return (n < 10 ? "0" : "") + n; }

  function iso(y, m, d) { return y + "-" + pad(m) + "-" + pad(d); }

  function todayJST() {
    var now = new Date();
    var jst = new Date(now.getTime() + (now.getTimezoneOffset() + 540) * 60000);
    return iso(jst.getFullYear(), jst.getMonth() + 1, jst.getDate());
  }

  function shiftDays(isoDate, delta) {
    var p = isoDate.split("-");
    var d = new Date(Date.UTC(+p[0], +p[1] - 1, +p[2]));
    d.setUTCDate(d.getUTCDate() + delta);
    return iso(d.getUTCFullYear(), d.getUTCMonth() + 1, d.getUTCDate());
  }

  function param(name) {
    var m = new RegExp("[?&]" + name + "=([^&#]*)").exec(location.search);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function banner(html, ok) {
    var old = document.getElementById("scirate-snap-banner");
    if (old) { old.remove(); }
    var el = document.createElement("div");
    el.id = "scirate-snap-banner";
    el.style.cssText =
      "position:fixed;z-index:2147483647;top:0;left:0;right:0;padding:14px 18px;" +
      "font:14px/1.6 -apple-system,BlinkMacSystemFont,sans-serif;color:#fff;" +
      "background:" + (ok ? "#1f7a3f" : "#8a5300") + ";box-shadow:0 2px 8px rgba(0,0,0,.3)";
    el.innerHTML =
      html +
      '<span style="float:right;cursor:pointer;font-weight:700" ' +
      'onclick="this.parentNode.remove()">&times;</span>';
    document.body.appendChild(el);
  }

  // ---- what period is this page? -----------------------------------------
  var range = parseInt(param("range"), 10);
  if (!range || range < 1) { range = 1; }
  var endDate = param("date") || todayJST();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(endDate)) {
    banner("URL の date= が YYYY-MM-DD ではありません: " + endDate, false);
    return;
  }
  var startDate = shiftDays(endDate, -(range - 1));
  var threshold = range === 1 ? 1 : 30;
  var storeKey = "scirate-snap:" + endDate + ":" + range;

  // ---- scrape what is already on screen -----------------------------------
  var rows = [];
  var minOnPage = null;
  var nodes = document.querySelectorAll("li.paper");
  for (var i = 0; i < nodes.length; i++) {
    var li = nodes[i];
    var toggle = li.querySelector(".scite-toggle[data-paper-uid]");
    var countEl = li.querySelector(".scites-count button, .scites-count a");
    var uidEl = li.querySelector(".uid");
    if (!toggle || !countEl || !uidEl) { continue; }

    var uid = (toggle.getAttribute("data-paper-uid") || "").trim();
    var scites = parseInt((countEl.textContent || "").replace(/[^0-9]/g, ""), 10);
    var dm = /([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{4})/.exec(uidEl.textContent || "");
    if (!uid || isNaN(scites) || !dm || !MONTHS[dm[1]]) { continue; }

    rows.push({
      uid: uid,
      scites_count: scites,
      pubdate: iso(+dm[3], MONTHS[dm[1]], +dm[2])
    });
    if (minOnPage === null || scites < minOnPage) { minOnPage = scites; }
  }

  if (!rows.length) {
    banner("この画面から論文行を読み取れませんでした。" +
           "scirate.com の一覧ページで実行してください。", false);
    return;
  }

  // ---- merge with rows collected from earlier pages ------------------------
  var store = {};
  try { store = JSON.parse(localStorage.getItem(storeKey) || "{}"); } catch (e) { store = {}; }
  for (var j = 0; j < rows.length; j++) { store[rows[j].uid] = rows[j]; }
  try { localStorage.setItem(storeKey, JSON.stringify(store)); } catch (e) { /* quota */ }

  var all = Object.keys(store).map(function (k) { return store[k]; });

  // Drop anything announced outside the requested period: SciRate can show a
  // neighbouring day when a paper is cross-listed later.
  var inPeriod = all.filter(function (r) {
    return r.pubdate >= startDate && r.pubdate <= endDate;
  });
  var dropped = all.length - inPeriod.length;

  // The bot rejects a snapshot that is not globally descending.
  inPeriod.sort(function (a, b) {
    if (b.scites_count !== a.scites_count) { return b.scites_count - a.scites_count; }
    return a.uid < b.uid ? -1 : a.uid > b.uid ? 1 : 0;
  });

  var kept = inPeriod.filter(function (r) { return r.scites_count >= threshold; });

  // ---- is the period actually finished? -----------------------------------
  if (minOnPage >= threshold) {
    banner(
      "<b>まだ完了していません。</b> この画面の最小 Scite 数は " + minOnPage +
      " で、しきい値 " + threshold + " 以上がまだ続いています。<br>" +
      "次のページへ進んでもう一度実行してください（累計 " + inPeriod.length + " 件を保持中）。",
      false);
    return;
  }

  var snapshot = {
    date: endDate,
    complete: true,
    range_days: range,
    period_start: startDate,
    period_end: endDate,
    generated_at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
    source: "scirate.com manual browser snapshot",
    papers: kept
  };
  var text = JSON.stringify(snapshot, null, 2);

  function done() {
    banner(
      "<b>スナップショットをコピーしました。</b> " + endDate + " / range=" + range +
      "d ・ " + kept.length + " 件（Scite " + threshold + "+）" +
      (dropped ? " ・ 期間外 " + dropped + " 件を除外" : "") + "<br>" +
      "ターミナルで <code style=\"background:rgba(255,255,255,.2);padding:1px 5px;" +
      "border-radius:3px\">python3 tools/scirate_snapshot.py</code> を実行してください。",
      true);
    try { localStorage.removeItem(storeKey); } catch (e) { /* ignore */ }
  }

  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, function () { manual(text); });
  } else {
    manual(text);
  }

  function manual(payload) {
    var ta = document.createElement("textarea");
    ta.value = payload;
    ta.style.cssText =
      "position:fixed;z-index:2147483647;top:60px;left:5%;width:90%;height:60%";
    document.body.appendChild(ta);
    ta.select();
    banner("クリップボードへ自動コピーできませんでした。" +
           "下のテキストを全選択してコピーしてください。", false);
  }
})();
