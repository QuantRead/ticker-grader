/* ═══════════════════════════════════════════════════════════
   QuantRead Ticker Grader — Frontend Logic (Freemium)
   ═══════════════════════════════════════════════════════════ */

const API_BASE = window.location.origin;
const PRO_URL = "https://quantread.app/indicators#ticker-grader-pro";

// ─── DOM References ────────────────────────────────────────
const tickerInput = document.getElementById("ticker-input");
const gradeBtn = document.getElementById("grade-btn");
const resultsSection = document.getElementById("results");
const errorBanner = document.getElementById("error-banner");
const errorText = document.getElementById("error-text");

// ─── Event Listeners ───────────────────────────────────────
tickerInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") gradeIt();
});

tickerInput.addEventListener("input", () => {
    tickerInput.value = tickerInput.value.toUpperCase();
    hideError();
});

// ─── Quick Grade (popular chip) ────────────────────────────
function quickGrade(ticker) {
    tickerInput.value = ticker;
    gradeIt();
}

// ─── Main Grade Function ───────────────────────────────────
async function gradeIt() {
    const ticker = tickerInput.value.trim().toUpperCase();
    if (!ticker) {
        showError("Please enter a ticker symbol.");
        return;
    }

    hideError();
    setLoading(true);

    try {
        const res = await fetch(`${API_BASE}/api/grade/${ticker}`);

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || `Failed to grade ${ticker}`);
        }

        const data = await res.json();
        renderResults(data);
    } catch (err) {
        showError(err.message);
        resultsSection.style.display = "none";
    } finally {
        setLoading(false);
    }
}

// ─── Render Results ────────────────────────────────────────
function renderResults(data) {
    const usage = data.usage || { remaining: 99, limit: 3, is_pro: false, limit_reached: false };

    // Header
    document.getElementById("res-ticker").textContent = data.ticker;
    document.getElementById("res-company").textContent = data.company_name;
    document.getElementById("res-sector").textContent = data.sector;
    document.getElementById("res-mcap").textContent = data.market_cap;
    document.getElementById("res-price").textContent = `$${data.price.toFixed(2)}`;

    const changeEl = document.getElementById("res-change");
    const sign = data.day_change >= 0 ? "+" : "";
    changeEl.textContent = `${sign}$${data.day_change.toFixed(2)} (${sign}${data.day_change_pct.toFixed(2)}%)`;
    changeEl.className = `price-change ${data.day_change >= 0 ? "positive" : "negative"}`;

    // Grade Ring (always visible — even for free users)
    const gradeCard = document.querySelector(".grade-card");
    gradeCard.className = `grade-card grade-${data.grade.toLowerCase()}`;

    document.getElementById("res-grade").textContent = data.grade;
    document.getElementById("res-score").textContent = data.score;
    document.getElementById("res-verdict").textContent = data.verdict;

    // Animate ring
    const ring = document.getElementById("grade-ring-fill");
    const circumference = 2 * Math.PI * 62;
    const offset = circumference - (data.score / 100) * circumference;
    ring.style.strokeDasharray = circumference;
    ring.style.strokeDashoffset = circumference;

    requestAnimationFrame(() => {
        setTimeout(() => {
            ring.style.strokeDashoffset = offset;
        }, 100);
    });

    // ─── RSI Penalty Warning ────────────────────────────────
    const rsiBanner = document.getElementById("rsi-penalty-banner");
    if (rsiBanner) {
        rsiBanner.style.display = data.rsi_penalty ? "block" : "none";
    }

    // ─── Data Source Badge ──────────────────────────────────
    const dsContainer = document.getElementById("grade-data-source");
    const dsBadge = document.getElementById("data-source-badge");
    if (dsContainer && dsBadge) {
        if (data.data_source === "intraday_5m") {
            dsBadge.textContent = "📡 LIVE — Intraday 5m Data";
            dsBadge.style.background = "rgba(16,185,129,0.12)";
            dsBadge.style.color = "#34d399";
            dsBadge.style.border = "1px solid rgba(16,185,129,0.3)";
        } else {
            dsBadge.textContent = "📊 Daily Data (Market Closed)";
            dsBadge.style.background = "rgba(156,163,175,0.12)";
            dsBadge.style.color = "#9ca3af";
            dsBadge.style.border = "1px solid rgba(156,163,175,0.3)";
        }
        dsContainer.style.display = "block";
    }

    // ─── Usage Counter ──────────────────────────────────────
    updateUsageCounter(usage);

    // ─── Indicators (gated) ─────────────────────────────────
    const indicatorsGrid = document.querySelector(".indicators-grid");
    const blurOverlay = document.getElementById("blur-overlay");
    const interpretSection = document.getElementById("interpret-section");

    if (data.indicators && !usage.limit_reached) {
        // Full access — render all indicators
        indicatorsGrid.classList.remove("blurred");
        if (blurOverlay) blurOverlay.style.display = "none";
        if (interpretSection) interpretSection.classList.remove("blurred");
        renderIndicators(data.indicators, data.price);
    } else {
        // Gated — show blurred indicators with upgrade CTA
        indicatorsGrid.classList.add("blurred");
        if (blurOverlay) blurOverlay.style.display = "flex";
        if (interpretSection) interpretSection.classList.add("blurred");

        // Set placeholder values for blurred state
        renderPlaceholderIndicators();
    }

    // Show results
    resultsSection.style.display = "block";
    resultsSection.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ─── Render Indicators (Full Access) ───────────────────────
function renderIndicators(ind, price) {
    // RS vs SPY (highest weight — matches trading agent)
    if (ind.rs_vs_spy) {
        const rsLabel = ind.rs_vs_spy.label;
        const rsBadgeClass = (rsLabel === 'LEADER' || rsLabel === 'STRONG') ? 'bull' : rsLabel === 'NEUTRAL' ? 'neutral' : 'bear';
        const rsBadge = document.getElementById('ind-rs-label');
        rsBadge.textContent = rsLabel;
        rsBadge.className = `ind-badge ${rsBadgeClass}`;
        document.getElementById('ind-rs-val').textContent = `${ind.rs_vs_spy.value}x`;
        const strengthLabels = { 'LEADER': 'Outperforming SPY', 'STRONG': 'Beating SPY', 'NEUTRAL': 'In-line with SPY', 'LAGGING': 'Trailing SPY', 'WEAK': 'Underperforming SPY' };
        document.getElementById('ind-rs-strength').textContent = strengthLabels[rsLabel] || rsLabel;
        setBar('ind-rs-bar', ind.rs_vs_spy.score, 5);
    }

    // EMA Ribbon
    setBadgeClass("ind-ribbon-status", ind.ema_ribbon.status);
    document.getElementById("ind-ribbon-status").textContent = ind.ema_ribbon.status;
    document.getElementById("ind-ema8").textContent = `$${ind.ema_ribbon.ema_8.toFixed(2)}`;
    document.getElementById("ind-ema21").textContent = `$${ind.ema_ribbon.ema_21.toFixed(2)}`;
    document.getElementById("ind-ema34").textContent = `$${ind.ema_ribbon.ema_34.toFixed(2)}`;
    document.getElementById("ind-ema55").textContent = `$${ind.ema_ribbon.ema_55.toFixed(2)}`;
    setBar("ind-ribbon-bar", ind.ema_ribbon.score, 5);

    // RVOL
    const rvolBadge = document.getElementById("ind-rvol-val");
    rvolBadge.textContent = `${ind.rvol.value}x`;
    rvolBadge.className = `ind-badge ${ind.rvol.value >= 1.5 ? "hot" : ind.rvol.value < 0.7 ? "cold" : "neutral"}`;
    document.getElementById("ind-cvol").textContent = formatVolume(ind.rvol.current_volume);
    document.getElementById("ind-avol").textContent = formatVolume(ind.rvol.avg_volume);
    setBar("ind-rvol-bar", ind.rvol.score, 5);

    // RSI
    setBadgeClass("ind-rsi-label", ind.rsi.label === "BULLISH" ? "BULL" : ind.rsi.label === "OVERSOLD" ? "BEAR" : ind.rsi.label === "OVERBOUGHT" ? "BEAR" : "NEUTRAL");
    document.getElementById("ind-rsi-label").textContent = ind.rsi.label;
    document.getElementById("ind-rsi-val").textContent = ind.rsi.value.toFixed(1);
    document.getElementById("ind-rsi-zone").textContent =
        ind.rsi.value > 70 ? "Overbought Zone" :
        ind.rsi.value < 30 ? "Oversold Zone" :
        ind.rsi.value >= 50 ? "Bullish Zone" : "Neutral Zone";
    setBar("ind-rsi-bar", ind.rsi.score, 5);

    // ATR
    document.getElementById("ind-atr-pct").textContent = `${ind.atr.pct}%`;
    document.getElementById("ind-atr-pct").className = "ind-badge neutral";
    document.getElementById("ind-atr-val").textContent = `$${ind.atr.value.toFixed(2)}`;
    document.getElementById("ind-atr-pctval").textContent = `${ind.atr.pct}%`;
    setBar("ind-atr-bar", ind.atr.score, 5);

    // Momentum
    const momBadge = document.getElementById("ind-mom-pct");
    momBadge.textContent = `${ind.momentum.five_day_pct >= 0 ? "+" : ""}${ind.momentum.five_day_pct}%`;
    momBadge.className = `ind-badge ${ind.momentum.five_day_pct > 0 ? "bull" : ind.momentum.five_day_pct < 0 ? "bear" : "neutral"}`;
    document.getElementById("ind-mom-val").textContent = `${ind.momentum.five_day_pct >= 0 ? "+" : ""}${ind.momentum.five_day_pct}%`;
    setBar("ind-mom-bar", ind.momentum.score, 5);

    // Trend
    setBadgeClass("ind-trend-status", ind.trend.status === "ABOVE" ? "BULL" : "BEAR");
    document.getElementById("ind-trend-status").textContent = ind.trend.status;
    document.getElementById("ind-sma20").textContent = `$${ind.trend.sma_20.toFixed(2)}`;
    const trendDiff = ((price - ind.trend.sma_20) / ind.trend.sma_20 * 100).toFixed(2);
    document.getElementById("ind-trend-diff").textContent = `${trendDiff >= 0 ? "+" : ""}${trendDiff}%`;
    setBar("ind-trend-bar", ind.trend.score, 5);
}

// ─── Render Placeholder Indicators (Blurred State) ─────────
function renderPlaceholderIndicators() {
    document.getElementById("ind-rs-label").textContent = "—";
    document.getElementById("ind-rs-val").textContent = "••••";
    document.getElementById("ind-rs-strength").textContent = "••••";
    document.getElementById("ind-ribbon-status").textContent = "—";
    document.getElementById("ind-ema8").textContent = "••••";
    document.getElementById("ind-ema21").textContent = "••••";
    document.getElementById("ind-ema34").textContent = "••••";
    document.getElementById("ind-ema55").textContent = "••••";
    document.getElementById("ind-rvol-val").textContent = "—";
    document.getElementById("ind-cvol").textContent = "••••";
    document.getElementById("ind-avol").textContent = "••••";
    document.getElementById("ind-rsi-label").textContent = "—";
    document.getElementById("ind-rsi-val").textContent = "••••";
    document.getElementById("ind-rsi-zone").textContent = "••••";
    document.getElementById("ind-atr-pct").textContent = "—";
    document.getElementById("ind-atr-val").textContent = "••••";
    document.getElementById("ind-atr-pctval").textContent = "••••";
    document.getElementById("ind-mom-pct").textContent = "—";
    document.getElementById("ind-mom-val").textContent = "••••";
    document.getElementById("ind-trend-status").textContent = "—";
    document.getElementById("ind-sma20").textContent = "••••";
    document.getElementById("ind-trend-diff").textContent = "••••";
}

// ─── Usage Counter ─────────────────────────────────────────
function updateUsageCounter(usage) {
    const counter = document.getElementById("usage-counter");
    if (!counter) return;

    if (usage.is_pro) {
        counter.innerHTML = `<span class="usage-pro">PRO</span> Unlimited grades`;
        counter.className = "usage-counter pro";
    } else {
        const remaining = usage.remaining;
        counter.innerHTML = `<span class="usage-dots">${getDots(remaining, usage.limit)}</span> ${remaining}/${usage.limit} free grades remaining today`;
        counter.className = `usage-counter ${remaining === 0 ? "depleted" : remaining === 1 ? "low" : "ok"}`;
    }
}

function getDots(remaining, total) {
    let dots = "";
    for (let i = 0; i < total; i++) {
        dots += i < remaining ? "●" : "○";
    }
    return dots;
}

// ─── Upgrade Modal ─────────────────────────────────────────
function openUpgradeModal() {
    const modal = document.getElementById("upgrade-modal");
    if (modal) modal.classList.add("active");
}

function closeUpgradeModal() {
    const modal = document.getElementById("upgrade-modal");
    if (modal) modal.classList.remove("active");
}

// Close modal on backdrop click
document.addEventListener("click", (e) => {
    if (e.target.id === "upgrade-modal") closeUpgradeModal();
});

document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeUpgradeModal();
});

// ─── Pro Verification ─────────────────────────────────────────
function toggleVerifyForm() {
    const form = document.getElementById("verify-pro-form");
    const toggle = document.getElementById("verify-pro-toggle");
    if (form.style.display === "none") {
        form.style.display = "block";
        toggle.style.display = "none";
        document.getElementById("verify-email-input").focus();
    } else {
        form.style.display = "none";
        toggle.style.display = "block";
    }
}

async function verifyPro() {
    const emailInput = document.getElementById("verify-email-input");
    const status = document.getElementById("verify-status");
    const btn = document.getElementById("verify-submit-btn");
    const email = emailInput.value.trim();

    if (!email || !email.includes("@")) {
        status.textContent = "Please enter a valid email address.";
        status.className = "verify-status error";
        return;
    }

    btn.disabled = true;
    btn.textContent = "Checking...";
    status.textContent = "";

    try {
        const res = await fetch(`${API_BASE}/api/verify-pro`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ email }),
        });

        const data = await res.json();

        if (data.is_pro) {
            status.textContent = "✅ Pro verified! Reloading...";
            status.className = "verify-status success";
            setTimeout(() => window.location.reload(), 1200);
        } else {
            status.textContent = data.message || "No active subscription found.";
            status.className = "verify-status error";
            btn.disabled = false;
            btn.textContent = "Verify";
        }
    } catch (err) {
        status.textContent = "Verification failed. Please try again.";
        status.className = "verify-status error";
        btn.disabled = false;
        btn.textContent = "Verify";
    }
}

// Allow Enter key in verify email input
document.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && document.activeElement?.id === "verify-email-input") {
        verifyPro();
    }
});

// ─── Helpers ───────────────────────────────────────────────_

function setLoading(loading) {
    gradeBtn.disabled = loading;
    gradeBtn.classList.toggle("loading", loading);
}

function showError(msg) {
    errorText.textContent = msg;
    errorBanner.style.display = "block";
}

function hideError() {
    errorBanner.style.display = "none";
}

function setBadgeClass(id, status) {
    const el = document.getElementById(id);
    const cls = status === "BULL" ? "bull" : status === "BEAR" ? "bear" : "neutral";
    el.className = `ind-badge ${cls}`;
}

function setBar(id, score, max) {
    const el = document.getElementById(id);
    const pct = (score / max) * 100;

    let colorClass = "green";
    if (pct < 40) colorClass = "red";
    else if (pct < 70) colorClass = "yellow";

    el.className = `ind-bar ${colorClass}`;
    setTimeout(() => {
        el.style.width = `${pct}%`;
    }, 200);
}

function formatVolume(vol) {
    if (vol >= 1_000_000) return `${(vol / 1_000_000).toFixed(1)}M`;
    if (vol >= 1_000) return `${(vol / 1_000).toFixed(0)}K`;
    return vol.toLocaleString();
}
