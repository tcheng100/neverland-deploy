const STORAGE_WALLETS_KEY = "neverland_dashboard_wallets_v1";
const STORAGE_POLL_KEY = "neverland_dashboard_poll_v1";

const state = {
  wallets: [],
  intervalMs: 15000,
  timer: null,
  prevSnapshot: null,
  alertFeed: [],
  emittedAlertKeys: new Set(),
  audioContext: null,
  prices: { dust_usd: 0, mon_usd: 0 },
};

const walletsInput = document.getElementById("walletsInput");
const pollInput = document.getElementById("pollInput");
const startBtn = document.getElementById("startBtn");
const saveWalletsBtn = document.getElementById("saveWalletsBtn");
const clearWalletsBtn = document.getElementById("clearWalletsBtn");
const notifyBtn = document.getElementById("notifyBtn");
const savedInfo = document.getElementById("savedInfo");

const statusBar = document.getElementById("statusBar");
const statusText = document.getElementById("statusText");
const statusMeta = document.getElementById("statusMeta");
const myCount = document.getElementById("myCount");
const walletCount = document.getElementById("walletCount");
const bestRank = document.getElementById("bestRank");
const threatCount = document.getElementById("threatCount");
const priceInputs = document.getElementById("priceInputs");
const alertsList = document.getElementById("alertsList");
const alertsCounter = document.getElementById("alertsCounter");
const alertsPanel = document.getElementById("alertsPanel");
const tableBody = document.getElementById("tableBody");

const calcDustLocked = document.getElementById("calcDustLocked");
const calcCompetitorDustPerMon = document.getElementById("calcCompetitorDustPerMon");
const calcUndercutMon = document.getElementById("calcUndercutMon");
const calcTargetDiscount = document.getElementById("calcTargetDiscount");
const calcSuggestedMon = document.getElementById("calcSuggestedMon");
const calcDustPerMon = document.getElementById("calcDustPerMon");
const calcDiscountAtSuggested = document.getElementById("calcDiscountAtSuggested");
const calcMonFromDiscount = document.getElementById("calcMonFromDiscount");

function parseWallets(text) {
  const raw = (text || "").replace(/[\n;]+/g, ",").split(",");
  const out = [];
  const seen = new Set();
  for (const item of raw) {
    const wallet = item.trim().toLowerCase();
    if (!wallet) continue;
    if (!wallet.startsWith("0x") || wallet.length !== 42) continue;
    if (seen.has(wallet)) continue;
    seen.add(wallet);
    out.push(wallet);
  }
  return out;
}

function fmtPct(value) {
  const num = Number(value || 0);
  return `${num >= 0 ? "+" : ""}${num.toFixed(2)}%`;
}

function shortAddr(addr) {
  if (!addr || addr.length < 12) return addr || "-";
  return `${addr.slice(0, 6)}...${addr.slice(-4)}`;
}

function setStatus(mode, text, meta = "") {
  statusBar.classList.remove("idle", "live", "error");
  statusBar.classList.add(mode);
  statusText.textContent = text;
  statusMeta.textContent = meta;
}

function saveWalletsToStorage(wallets) {
  localStorage.setItem(STORAGE_WALLETS_KEY, JSON.stringify(wallets));
}

function loadWalletsFromStorage() {
  try {
    const raw = localStorage.getItem(STORAGE_WALLETS_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parseWallets(parsed.join(","));
  } catch {
    return [];
  }
}

function updateSavedInfo(wallets) {
  savedInfo.textContent = `Saved wallets: ${wallets.length}`;
}

function persistPollValue() {
  const sec = Math.max(5, Number(pollInput.value || 15));
  localStorage.setItem(STORAGE_POLL_KEY, String(sec));
}

function loadPollValue() {
  const raw = localStorage.getItem(STORAGE_POLL_KEY);
  const sec = Math.max(5, Number(raw || 15));
  pollInput.value = String(sec);
}

async function requestNotificationPermission() {
  if (!("Notification" in window)) {
    notifyBtn.textContent = "Notifications Unsupported";
    return;
  }
  const permission = await Notification.requestPermission();
  notifyBtn.textContent = permission === "granted" ? "Notifications Enabled" : "Notifications Blocked";
}

function beep() {
  if (!window.AudioContext && !window.webkitAudioContext) return;
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!state.audioContext) state.audioContext = new Ctx();
  const ctx = state.audioContext;

  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.type = "square";
  osc.frequency.value = 920;
  gain.gain.value = 0.001;
  osc.connect(gain);
  gain.connect(ctx.destination);

  const now = ctx.currentTime;
  gain.gain.exponentialRampToValueAtTime(0.15, now + 0.01);
  gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.23);
  osc.start(now);
  osc.stop(now + 0.24);
}

function fireBrowserNotification(alert) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  new Notification("Discount Threat Alert", {
    body: `${alert.competitorToken} moved above #${alert.myToken} (${shortAddr(alert.mySeller)})`,
    tag: `${alert.myToken}-${alert.competitorToken}-${alert.mySeller}`,
  });
}

function addJump(el) {
  el.classList.remove("jump");
  void el.offsetWidth;
  el.classList.add("jump");
}

function addAlert(alert) {
  state.alertFeed.unshift(alert);
  if (state.alertFeed.length > 180) state.alertFeed.pop();
  alertsCounter.textContent = String(state.alertFeed.length);

  const item = document.createElement("article");
  item.className = "alert-item";
  item.innerHTML = `
    <div class="alert-time">${new Date(alert.time * 1000).toLocaleTimeString()} · ${shortAddr(alert.mySeller)}</div>
    <div>
      Token <strong>#${alert.myToken}</strong> is now below <strong>#${alert.competitorToken}</strong>
      (${fmtPct(alert.competitorDiscount)} vs ${fmtPct(alert.myDiscount)}).
    </div>
  `;
  alertsList.prepend(item);
  if (alertsList.children.length > 180) alertsList.lastElementChild.remove();

  beep();
  fireBrowserNotification(alert);
  addJump(alertsPanel);
}

function detectNewThreatAlerts(prev, curr, wallets) {
  if (!prev) return [];
  const tracked = new Set(wallets.map((w) => w.toLowerCase()));

  const prevByKey = new Map();
  for (const row of prev.my_listings || []) {
    prevByKey.set(`${row.token_id}:${row.seller}`, row);
  }

  const currByKey = new Map();
  for (const row of curr.my_listings || []) {
    currByKey.set(`${row.token_id}:${row.seller}`, row);
  }

  const prevThreatByKey = new Map();
  for (const threat of prev.threats || []) {
    prevThreatByKey.set(`${threat.my_token}:${threat.my_seller}`, threat);
  }

  const currThreatByKey = new Map();
  for (const threat of curr.threats || []) {
    currThreatByKey.set(`${threat.my_token}:${threat.my_seller}`, threat);
  }

  const alerts = [];
  for (const [key, myRow] of currByKey.entries()) {
    const threat = currThreatByKey.get(key);
    if (!threat) continue;
    if (tracked.has((threat.competitor_seller || "").toLowerCase())) continue;

    const prevThreat = prevThreatByKey.get(key);
    const isNewThreat = !prevThreat || prevThreat.competitor_token !== threat.competitor_token;
    const prevMine = prevByKey.get(key);
    const droppedRank = prevMine && Number(myRow.rank_discount) > Number(prevMine.rank_discount);

    if (isNewThreat || droppedRank) {
      const alertKey = `${key}:${threat.competitor_token}:${threat.competitor_rank}`;
      if (!state.emittedAlertKeys.has(alertKey)) {
        state.emittedAlertKeys.add(alertKey);
        alerts.push({
          key: alertKey,
          time: curr.captured_at,
          myToken: threat.my_token,
          mySeller: threat.my_seller,
          competitorToken: threat.competitor_token,
          myDiscount: threat.my_discount_pct,
          competitorDiscount: threat.competitor_discount_pct,
        });
      }
    }
  }
  return alerts;
}

function renderMetrics(snapshot) {
  myCount.textContent = String(snapshot.my_listing_count ?? 0);
  walletCount.textContent = String(snapshot.tracked_wallet_count ?? state.wallets.length);
  bestRank.textContent = snapshot.my_best_rank ? `#${snapshot.my_best_rank}` : "-";
  threatCount.textContent = String((snapshot.threats || []).length);

  const dust = Number(snapshot.prices?.dust_usd || 0).toFixed(4);
  const mon = Number(snapshot.prices?.mon_usd || 0).toFixed(5);
  const usingFallback = Boolean(snapshot.using_fallback_source);
  const fallbackFields = Array.isArray(snapshot.fallback_fields) ? snapshot.fallback_fields : [];
  const fallbackText = usingFallback
    ? ` [FALLBACK: ${fallbackFields.length ? fallbackFields.join(", ") : "cached_price"}]`
    : "";
  priceInputs.textContent = `DUST $${dust} / MON $${mon}${fallbackText}`;
  priceInputs.classList.toggle("fallback-on", usingFallback);
}

function renderTable(snapshot) {
  const mineKeys = new Set((snapshot.my_listings || []).map((x) => `${x.token_id}:${x.seller}`));
  const threatTokens = new Set((snapshot.threats || []).map((x) => x.competitor_token));
  const rows = snapshot.listings || [];
  const view = rows.slice(0, 180);

  tableBody.innerHTML = "";
  for (const row of view) {
    const key = `${row.token_id}:${row.seller}`;
    const tr = document.createElement("tr");
    if (mineKeys.has(key)) tr.classList.add("mine");
    if (threatTokens.has(row.token_id)) tr.classList.add("threat");

    const discountClass = Number(row.discount_pct) >= 0 ? "discount-pos" : "discount-neg";
    tr.innerHTML = `
      <td>${row.rank_discount}</td>
      <td><a href="${row.asset_url}" target="_blank" rel="noopener">#${row.token_id}</a></td>
      <td>${shortAddr(row.seller)}</td>
      <td class="${discountClass}">${fmtPct(row.discount_pct)}</td>
      <td>${Number(row.price_mon).toFixed(3)}</td>
      <td>${Number(row.dust_per_mon || 0).toFixed(5)}</td>
      <td>${Number(row.dust_locked).toFixed(2)}</td>
    `;
    tableBody.appendChild(tr);
  }
}

function calculateReprice() {
  const dustLocked = Number(calcDustLocked.value || 0);
  const competitorDustPerMon = Number(calcCompetitorDustPerMon.value || 0);
  const competitorMon = dustLocked > 0 && competitorDustPerMon > 0 ? dustLocked / competitorDustPerMon : 0;
  const undercutMon = Number(calcUndercutMon.value || 0);
  const targetDiscount = calcTargetDiscount.value === "" ? null : Number(calcTargetDiscount.value);

  const suggestedMon = Math.max(competitorMon - undercutMon, 0);
  calcSuggestedMon.textContent = suggestedMon > 0 ? suggestedMon.toFixed(6) : "-";

  if (dustLocked > 0 && suggestedMon > 0) {
    calcDustPerMon.textContent = (dustLocked / suggestedMon).toFixed(7);
  } else {
    calcDustPerMon.textContent = "-";
  }

  const dustUsd = Number(state.prices.dust_usd || 0);
  const monUsd = Number(state.prices.mon_usd || 0);
  if (dustLocked > 0 && suggestedMon > 0 && dustUsd > 0 && monUsd > 0) {
    const dustValueUsd = dustLocked * dustUsd;
    const listingUsd = suggestedMon * monUsd;
    const discountPct = ((dustValueUsd - listingUsd) / dustValueUsd) * 100;
    calcDiscountAtSuggested.textContent = fmtPct(discountPct);
  } else {
    calcDiscountAtSuggested.textContent = "-";
  }

  if (
    targetDiscount !== null &&
    Number.isFinite(targetDiscount) &&
    dustLocked > 0 &&
    dustUsd > 0 &&
    monUsd > 0
  ) {
    const dustValueUsd = dustLocked * dustUsd;
    const targetListingUsd = dustValueUsd * (1 - targetDiscount / 100);
    const monFromTarget = targetListingUsd / monUsd;
    calcMonFromDiscount.textContent = Number.isFinite(monFromTarget) ? monFromTarget.toFixed(6) : "-";
  } else {
    calcMonFromDiscount.textContent = "-";
  }
}

async function fetchSnapshot() {
  const params = new URLSearchParams({
    wallets: state.wallets.join(","),
    slug: "voting-escrow-dust",
    limit: "200",
    max_pages: "20",
  });
  const res = await fetch(`/api/snapshot?${params.toString()}`);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "Snapshot fetch failed");
  return data;
}

async function tick() {
  try {
    setStatus("live", "Polling...", `every ${Math.floor(state.intervalMs / 1000)}s`);
    const snapshot = await fetchSnapshot();

    state.prices = snapshot.prices || state.prices;
    renderMetrics(snapshot);
    renderTable(snapshot);
    calculateReprice();

    const alerts = detectNewThreatAlerts(state.prevSnapshot, snapshot, state.wallets);
    for (const alert of alerts) addAlert(alert);

    state.prevSnapshot = snapshot;
    const fallbackFields = Array.isArray(snapshot.fallback_fields) ? snapshot.fallback_fields : [];
    const fallbackMeta = snapshot.using_fallback_source
      ? ` · FALLBACK ${fallbackFields.length ? fallbackFields.join(", ") : "cached_price"}`
      : "";
    setStatus(
      "live",
      "Live",
      `${new Date(snapshot.captured_at * 1000).toLocaleTimeString()} · ${snapshot.total_listings} listings${fallbackMeta}`
    );
  } catch (err) {
    setStatus("error", "Error", String(err.message || err));
  }
}

function startMonitoring() {
  const wallets = parseWallets(walletsInput.value);
  if (!wallets.length) {
    setStatus("error", "Invalid wallets", "Add at least one valid 0x wallet");
    return;
  }
  const sec = Math.max(5, Number(pollInput.value || 15));
  state.wallets = wallets;
  state.intervalMs = sec * 1000;
  state.prevSnapshot = null;
  state.emittedAlertKeys.clear();
  alertsList.innerHTML = "";
  alertsCounter.textContent = "0";

  if (state.timer) clearInterval(state.timer);
  tick();
  state.timer = setInterval(tick, state.intervalMs);
  setStatus("live", "Started", `${wallets.length} wallets · poll ${sec}s`);
}

function saveWallets() {
  const wallets = parseWallets(walletsInput.value);
  if (!wallets.length) {
    setStatus("error", "No valid wallets", "Nothing saved");
    return;
  }
  saveWalletsToStorage(wallets);
  updateSavedInfo(wallets);
  setStatus("live", "Wallets saved", `${wallets.length} addresses stored locally`);
}

function clearWallets() {
  localStorage.removeItem(STORAGE_WALLETS_KEY);
  walletsInput.value = "";
  updateSavedInfo([]);
  setStatus("idle", "Saved wallets cleared");
}

function bootstrapSavedState() {
  const savedWallets = loadWalletsFromStorage();
  walletsInput.value = savedWallets.join("\n");
  updateSavedInfo(savedWallets);
  loadPollValue();
  calculateReprice();
}

startBtn.addEventListener("click", startMonitoring);
saveWalletsBtn.addEventListener("click", saveWallets);
clearWalletsBtn.addEventListener("click", clearWallets);
notifyBtn.addEventListener("click", requestNotificationPermission);
pollInput.addEventListener("change", persistPollValue);

[calcDustLocked, calcCompetitorDustPerMon, calcUndercutMon, calcTargetDiscount].forEach((el) => {
  el.addEventListener("input", calculateReprice);
});

bootstrapSavedState();
setStatus("idle", "Idle", "Save wallets and press Start");
