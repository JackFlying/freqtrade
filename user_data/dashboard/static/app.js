const COLORS = {
    grid: "#e7ecf0",
    axis: "#687783",
    up: "#0a9b61",
    down: "#dc3f56",
    ma7: "#0788b5",
    ma20: "#7c52c7",
    ma99: "#d34278",
    crosshair: "#8797a2",
};

const CANDLE_CACHE_TTL_MS = 30_000;
const BACKTEST_TRADE_PAGE_SIZE = 100;
const AUTH_TOKEN_KEY = "trend-console-access-token";

const STRATEGY_PRESETS = {
    "1d": {
        label: "日K策略",
        lookback_days: 3,
        min_change_20d: 10,
        max_change_20d: 50,
        max_drawdown_to_gain_ratio_pct: 70,
        use_4h_ma_filter: false,
        use_ma99_filter: true,
        ma7_reclaim_enabled: true,
        ma7_reclaim_tolerance_pct: 1,
        ma7_reclaim_lookback_days: 2,
        ma7_exit_threshold_pct: 2.5,
        hard_stoploss_pct: 5,
        dynamic_drawdown_stop_enabled: true,
        dynamic_drawdown_activation_pct: 1.5,
        dynamic_max_profit_giveback_pct: 2,
        chandelier_exit_enabled: false,
        partial_take_profit_enabled: false,
        cooldown_enabled: true,
        cooldown_hours: 4,
        candidate_scan_interval_hours: 0.5,
    },
    "4h": {
        label: "4hK线策略",
        max_open_trades: 1,
        lookback_days: 2,
        min_change_20d: 6,
        max_change_20d: 30,
        max_drawdown_to_gain_ratio_pct: 50,
        use_4h_ma_filter: true,
        use_ma99_filter: true,
        ma7_reclaim_enabled: true,
        ma7_reclaim_tolerance_pct: 2,
        ma7_reclaim_lookback_days: 2,
        ma7_exit_threshold_pct: 4,
        hard_stoploss_pct: 12,
        dynamic_drawdown_stop_enabled: false,
        dynamic_drawdown_activation_pct: 1.5,
        dynamic_max_profit_giveback_pct: 2,
        chandelier_exit_enabled: true,
        partial_take_profit_enabled: true,
        cooldown_enabled: true,
        cooldown_hours: 4,
        candidate_scan_interval_hours: 4,
    },
};

const state = {
    candidates: [],
    filteredCandidates: [],
    selectedPair: null,
    sortKey: "default",
    timeframe: "1d",
    candles: [],
    candleCache: new Map(),
    chartRequest: 0,
    hoverIndex: null,
    viewCount: null,
    viewEnd: null,
    dragging: false,
    dragStartX: 0,
    dragStartEnd: 0,
    trackpadPanRemainder: 0,
    settingsDirty: false,
    backtestResult: null,
    backtestPollTimer: null,
    backtestHoverIndex: null,
    backtestTrades: [],
    backtestVisibleTradeCount: 0,
    settingsTab: "filter",
};
let appInitialized = false;

const elements = {
    authScreen: document.querySelector("#authScreen"),
    appShell: document.querySelector("#appShell"),
    loginForm: document.querySelector("#loginForm"),
    loginUsername: document.querySelector("#loginUsername"),
    loginPassword: document.querySelector("#loginPassword"),
    loginError: document.querySelector("#loginError"),
    logoutButton: document.querySelector("#logoutButton"),
    statusDot: document.querySelector("#statusDot"),
    statusText: document.querySelector("#statusText"),
    updatedAt: document.querySelector("#updatedAt"),
    candidateCount: document.querySelector("#candidateCount"),
    previewBadge: document.querySelector("#previewBadge"),
    candidateList: document.querySelector("#candidateList"),
    sortSelect: document.querySelector("#sortSelect"),
    refreshButton: document.querySelector("#refreshButton"),
    refreshButtonLabel: document.querySelector("#refreshButton span"),
    entryEnabledInput: document.querySelector("#entryEnabledInput"),
    maxOpenTradesInput: document.querySelector("#maxOpenTradesInput"),
    strategyTimeframeInput: document.querySelector("#strategyTimeframeInput"),
    entryEnabledLabel: document.querySelector("#entryEnabledLabel"),
    entryEnabledBadge: document.querySelector("#entryEnabledBadge"),
    entryEnabledRule: document.querySelector("#entryEnabledRule"),
    scanIntervalInput: document.querySelector("#scanIntervalInput"),
    lookbackDaysInput: document.querySelector("#lookbackDaysInput"),
    minChange20dInput: document.querySelector("#minChange20dInput"),
    maxChange20dInput: document.querySelector("#maxChange20dInput"),
    maxDrawdownToGainRatioInput: document.querySelector("#maxDrawdownToGainRatioInput"),
    useMa99FilterInput: document.querySelector("#useMa99FilterInput"),
    ma7ReclaimEnabledInput: document.querySelector("#ma7ReclaimEnabledInput"),
    ma7ReclaimEnabledLabel: document.querySelector("#ma7ReclaimEnabledLabel"),
    ma7ReclaimToleranceInput: document.querySelector("#ma7ReclaimToleranceInput"),
    ma7ReclaimLookbackInput: document.querySelector("#ma7ReclaimLookbackInput"),
    ma7ReclaimLookbackUnit: document.querySelector("#ma7ReclaimLookbackUnit"),
    ma7ExitThresholdInput: document.querySelector("#ma7ExitThresholdInput"),
    hardStoplossInput: document.querySelector("#hardStoplossInput"),
    noProgressExitEnabledInput: document.querySelector("#noProgressExitEnabledInput"),
    noProgressExitEnabledLabel: document.querySelector("#noProgressExitEnabledLabel"),
    drawdownStopModeInput: document.querySelector("#drawdownStopModeInput"),
    staticDrawdownSetting: document.querySelector("#staticDrawdownSetting"),
    dynamicDrawdownSetting: document.querySelector("#dynamicDrawdownSetting"),
    dynamicProfitGivebackSetting: document.querySelector("#dynamicProfitGivebackSetting"),
    peakDrawdownStopInput: document.querySelector("#peakDrawdownStopInput"),
    dynamicDrawdownActivationInput: document.querySelector("#dynamicDrawdownActivationInput"),
    dynamicMaxProfitGivebackInput: document.querySelector("#dynamicMaxProfitGivebackInput"),
    chandelierExitEnabledInput: document.querySelector(
        "#chandelierExitEnabledInput",
    ),
    chandelierExitEnabledLabel: document.querySelector(
        "#chandelierExitEnabledLabel",
    ),
    partialTakeProfitEnabledInput: document.querySelector(
        "#partialTakeProfitEnabledInput",
    ),
    partialTakeProfitEnabledLabel: document.querySelector(
        "#partialTakeProfitEnabledLabel",
    ),
    cooldownEnabledInput: document.querySelector("#cooldownEnabledInput"),
    cooldownEnabledLabel: document.querySelector("#cooldownEnabledLabel"),
    candidateReentryRequiredInput: document.querySelector("#candidateReentryRequiredInput"),
    candidateReentryRequiredLabel: document.querySelector("#candidateReentryRequiredLabel"),
    cooldownDurationSetting: document.querySelector("#cooldownDurationSetting"),
    cooldownHoursInput: document.querySelector("#cooldownHoursInput"),
    saveSettingsButton: document.querySelector("#saveSettingsButton"),
    settingsStatus: document.querySelector("#settingsStatus"),
    settingsTabs: document.querySelector("#settingsTabs"),
    settingsTabPanels: document.querySelectorAll("[data-settings-panel]"),
    drawdownProtectionDetails: document.querySelector(
        "#drawdownProtectionDetails",
    ),
    advancedExitDetails: document.querySelector("#advancedExitDetails"),
    minChangeRule: document.querySelector("#minChangeRule"),
    maxChangeRule: document.querySelector("#maxChangeRule"),
    maxDrawdownToGainRatioRule: document.querySelector("#maxDrawdownToGainRatioRule"),
    maFilterRule: document.querySelector("#maFilterRule"),
    ma99FilterLabel: document.querySelector("#ma99FilterLabel"),
    ma99RuleSuffix: document.querySelector("#ma99RuleSuffix"),
    ma7ExitThresholdRule: document.querySelector("#ma7ExitThresholdRule"),
    ma7ExitThresholdTimeframe: document.querySelector(
        "#ma7ExitThresholdTimeframe",
    ),
    ma7ExitRuleTimeframe: document.querySelector("#ma7ExitRuleTimeframe"),
    hardStoplossRule: document.querySelector("#hardStoplossRule"),
    noProgressExitRule: document.querySelector("#noProgressExitRule"),
    peakDrawdownStopRule: document.querySelector("#peakDrawdownStopRule"),
    dynamicDrawdownStopRule: document.querySelector("#dynamicDrawdownStopRule"),
    dynamicDrawdownActivationRule: document.querySelector("#dynamicDrawdownActivationRule"),
    chandelierExitRule: document.querySelector("#chandelierExitRule"),
    partialTakeProfitRule: document.querySelector("#partialTakeProfitRule"),
    cooldownRule: document.querySelector("#cooldownRule"),
    cooldownRuleSection: document.querySelector("#cooldownRuleSection"),
    cooldownRuleValue: document.querySelector("#cooldownRuleValue"),
    candidateReentryRequiredRule: document.querySelector("#candidateReentryRequiredRule"),
    peakDrawdownStopRuleValue: document.querySelector("#peakDrawdownStopRuleValue"),
    lookbackDaysTexts: document.querySelectorAll(".lookback-days-text"),
    searchInput: document.querySelector("#searchInput"),
    pairName: document.querySelector("#pairName"),
    provisionalBadge: document.querySelector("#provisionalBadge"),
    timeframeTabs: document.querySelector("#timeframeTabs"),
    latestPrice: document.querySelector("#latestPrice"),
    priceChange: document.querySelector("#priceChange"),
    ma7Value: document.querySelector("#ma7Value"),
    ma20Value: document.querySelector("#ma20Value"),
    ma99Value: document.querySelector("#ma99Value"),
    change20dValue: document.querySelector("#change20dValue"),
    drawdown20dValue: document.querySelector("#drawdown20dValue"),
    changePeriodLabel: document.querySelector("#changePeriodLabel"),
    drawdownPeriodLabel: document.querySelector("#drawdownPeriodLabel"),
    volume24h: document.querySelector("#volume24h"),
    chartCanvas: document.querySelector("#chartCanvas"),
    chartLoading: document.querySelector("#chartLoading"),
    chartTooltip: document.querySelector("#chartTooltip"),
    chartCard: document.querySelector(".chart-card"),
    refreshChartButton: document.querySelector("#refreshChartButton"),
    resetChartButton: document.querySelector("#resetChartButton"),
    zoomOutButton: document.querySelector("#zoomOutButton"),
    zoomInButton: document.querySelector("#zoomInButton"),
    viewTabs: document.querySelector("#viewTabs"),
    liveWorkspace: document.querySelector("#liveWorkspace"),
    backtestWorkspace: document.querySelector("#backtestWorkspace"),
    backtestDaysInput: document.querySelector("#backtestDaysInput"),
    backtestInitialBalanceInput: document.querySelector("#backtestInitialBalanceInput"),
    runBacktestButton: document.querySelector("#runBacktestButton"),
    cancelBacktestButton: document.querySelector("#cancelBacktestButton"),
    backtestStatusText: document.querySelector("#backtestStatusText"),
    backtestStatusMeta: document.querySelector("#backtestStatusMeta"),
    backtestProgressBar: document.querySelector("#backtestProgressBar"),
    backtestProgressText: document.querySelector("#backtestProgressText"),
    backtestTotalProfit: document.querySelector("#backtestTotalProfit"),
    backtestEndingBalance: document.querySelector("#backtestEndingBalance"),
    backtestMaxDrawdown: document.querySelector("#backtestMaxDrawdown"),
    backtestWinRate: document.querySelector("#backtestWinRate"),
    backtestTradeCount: document.querySelector("#backtestTradeCount"),
    backtestProfitFactor: document.querySelector("#backtestProfitFactor"),
    backtestDateRange: document.querySelector("#backtestDateRange"),
    backtestUniverseMeta: document.querySelector("#backtestUniverseMeta"),
    backtestTradeMeta: document.querySelector("#backtestTradeMeta"),
    monthlyReturnsBody: document.querySelector("#monthlyReturnsBody"),
    backtestEquityCanvas: document.querySelector("#backtestEquityCanvas"),
    backtestEquityTooltip: document.querySelector("#backtestEquityTooltip"),
    backtestEmpty: document.querySelector("#backtestEmpty"),
    backtestTableWrap: document.querySelector(".backtest-table-wrap"),
    backtestTradesBody: document.querySelector("#backtestTradesBody"),
};

function formatCompact(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return "--";
    const number = Number(value);
    if (Math.abs(number) >= 1e9) return `${(number / 1e9).toFixed(2)}B`;
    if (Math.abs(number) >= 1e6) return `${(number / 1e6).toFixed(2)}M`;
    if (Math.abs(number) >= 1e3) return `${(number / 1e3).toFixed(1)}K`;
    return number.toFixed(2);
}

function formatQuoteVolume(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return "--";
    const number = Number(value);
    if (Math.abs(number) >= 1e8) return `${(number / 1e8).toFixed(2)}亿`;
    if (Math.abs(number) >= 1e4) return `${(number / 1e4).toFixed(2)}万`;
    return number.toFixed(2);
}

function priceDecimals(value) {
    const number = Math.abs(Number(value));
    if (number >= 1000) return 2;
    if (number >= 10) return 3;
    if (number >= 1) return 4;
    if (number >= 0.01) return 5;
    return 8;
}

function formatPrice(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return "--";
    const number = Number(value);
    return number.toLocaleString("zh-CN", {
        minimumFractionDigits: 0,
        maximumFractionDigits: priceDecimals(number),
    });
}

function formatUpdatedAt(value) {
    if (!value) return "--";
    return new Intl.DateTimeFormat("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
    }).format(new Date(value));
}

function selectedCandidate() {
    return state.candidates.find((candidate) => candidate.pair === state.selectedPair);
}

function setConnectionStatus(kind, text) {
    elements.statusDot.className = `status-dot ${kind}`;
    elements.statusText.textContent = text;
}

async function fetchJson(url, options = {}) {
    const headers = new Headers(options.headers || {});
    const token = window.localStorage.getItem(AUTH_TOKEN_KEY);
    if (token && !headers.has("Authorization")) {
        headers.set("Authorization", `Bearer ${token}`);
    }
    const response = await fetch(url, {
        cache: "no-store",
        ...options,
        headers,
    });
    if (response.status === 401) {
        showLogin("登录已失效，请重新登录");
    }
    if (!response.ok) {
        let detail = `${response.status} ${response.statusText}`;
        try {
            const payload = await response.json();
            const apiDetail = payload.detail;
            if (typeof apiDetail === "string") {
                detail = apiDetail;
            } else if (Array.isArray(apiDetail)) {
                detail = apiDetail
                    .map((item) => item.msg || JSON.stringify(item))
                    .join("；");
            } else if (apiDetail) {
                detail = JSON.stringify(apiDetail);
            }
        } catch {
            // Keep the HTTP status when the response is not JSON.
        }
        throw new Error(detail);
    }
    return response.json();
}

function showLogin(message = "") {
    window.localStorage.removeItem(AUTH_TOKEN_KEY);
    elements.appShell.classList.add("is-hidden");
    elements.authScreen.classList.remove("is-hidden");
    elements.loginError.textContent = message;
    elements.loginError.hidden = !message;
    elements.loginPassword.value = "";
    elements.loginUsername.focus();
}

function showApp() {
    elements.authScreen.classList.add("is-hidden");
    elements.appShell.classList.remove("is-hidden");
}

async function submitLogin(event) {
    event.preventDefault();
    const username = elements.loginUsername.value.trim();
    const password = elements.loginPassword.value;
    elements.loginError.hidden = true;
    const button = elements.loginForm.querySelector("button[type=submit]");
    button.disabled = true;
    button.textContent = "登录中...";
    try {
        const response = await fetch("/api/auth/login", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username, password }),
        });
        const payload = await response.json();
        if (!response.ok) {
            throw new Error(payload.detail || "登录失败");
        }
        window.localStorage.setItem(AUTH_TOKEN_KEY, payload.access_token);
        elements.loginPassword.value = "";
        showApp();
        initializeApp();
    } catch (error) {
        elements.loginError.textContent = error.message;
        elements.loginError.hidden = false;
    } finally {
        button.disabled = false;
        button.textContent = "登录";
    }
}

async function bootstrapAuth() {
    const token = window.localStorage.getItem(AUTH_TOKEN_KEY);
    if (!token) {
        showLogin();
        return;
    }
    try {
        await fetchJson("/api/auth/me");
        showApp();
        initializeApp();
    } catch {
        showLogin("登录已失效，请重新登录");
    }
}

async function loadCandidates({
    preserveSelection = true,
    reloadChart = false,
} = {}) {
    try {
        const payload = await fetchJson("/api/candidates");
        state.candidates = payload.candidates || [];
        elements.candidateCount.textContent = String(state.candidates.length);
        elements.previewBadge.hidden = !payload.is_preview;
        elements.updatedAt.textContent = formatUpdatedAt(payload.status?.updated_at);
        elements.updatedAt.title = payload.status?.updated_at || "";
        setConnectionStatus(
            "online",
            payload.is_preview ? "预览结果，尚未用于交易" : "正式候选",
        );

        const previousPair = preserveSelection ? state.selectedPair : null;
        const stillExists = state.candidates.some((item) => item.pair === previousPair);
        state.selectedPair = stillExists ? previousPair : state.candidates[0]?.pair || null;
        const selectionChanged = state.selectedPair !== previousPair;
        filterCandidates();

        if (state.selectedPair) {
            updateCandidateMetrics();
            if (reloadChart || selectionChanged || !state.candles.length) {
                await loadChart({ resetView: true });
            }
        } else {
            elements.pairName.textContent = "--";
            elements.chartLoading.textContent = "当前没有满足条件的候选币";
            elements.chartLoading.classList.remove("hidden");
        }
    } catch (error) {
        setConnectionStatus("error", "连接异常");
        elements.candidateList.innerHTML = "";
        const message = document.createElement("div");
        message.className = "empty-state";
        message.textContent = `候选列表加载失败：${error.message}`;
        elements.candidateList.append(message);
    }
}

async function loadSettings() {
    try {
        const settings = await fetchJson("/api/settings");
        elements.strategyTimeframeInput.value = settings.strategy_timeframe || "1d";
        const scanSeconds = Number(settings.candidate_scan_interval_seconds);
        elements.scanIntervalInput.value = Number.isFinite(scanSeconds)
            ? Math.round((scanSeconds / 3600) * 10) / 10
            : "";
        elements.entryEnabledInput.checked = settings.entry_enabled;
        elements.maxOpenTradesInput.value = settings.max_open_trades ?? 1;
        elements.lookbackDaysInput.value = settings.lookback_days;
        elements.minChange20dInput.value = settings.min_change_20d;
        elements.maxChange20dInput.value = settings.max_change_20d;
        elements.maxDrawdownToGainRatioInput.value =
            settings.max_drawdown_to_gain_ratio_pct;
        elements.useMa99FilterInput.checked = settings.use_ma99_filter;
        elements.ma7ReclaimEnabledInput.checked =
            settings.ma7_reclaim_enabled ?? true;
        elements.ma7ReclaimToleranceInput.value =
            settings.ma7_reclaim_tolerance_pct ?? 1;
        elements.ma7ReclaimLookbackInput.value =
            settings.ma7_reclaim_lookback_days ?? 2;
        elements.ma7ExitThresholdInput.value = settings.ma7_exit_threshold_pct;
        elements.hardStoplossInput.value = settings.hard_stoploss_pct;
        elements.noProgressExitEnabledInput.checked =
            settings.no_progress_exit_enabled;
        elements.drawdownStopModeInput.value =
            settings.dynamic_drawdown_stop_enabled
                ? "dynamic"
                : settings.peak_drawdown_stop_enabled
                    ? "static"
                    : "off";
        elements.peakDrawdownStopInput.value = settings.peak_drawdown_stop_pct;
        elements.dynamicDrawdownActivationInput.value =
            settings.dynamic_drawdown_activation_pct;
        elements.dynamicMaxProfitGivebackInput.value =
            settings.dynamic_max_profit_giveback_pct;
        elements.chandelierExitEnabledInput.checked =
            settings.chandelier_exit_enabled ?? false;
        elements.partialTakeProfitEnabledInput.checked =
            settings.partial_take_profit_enabled ?? false;
        elements.drawdownProtectionDetails.open =
            elements.drawdownStopModeInput.value !== "off";
        elements.advancedExitDetails.open =
            elements.chandelierExitEnabledInput.checked
            || elements.partialTakeProfitEnabledInput.checked;
        elements.cooldownEnabledInput.checked = settings.cooldown_enabled;
        elements.candidateReentryRequiredInput.checked =
            settings.candidate_reentry_required;
        elements.cooldownHoursInput.value = settings.cooldown_hours;
        updateFilterRuleValues();
        state.settingsDirty = false;
        elements.saveSettingsButton.disabled = true;
    } catch (error) {
        elements.settingsStatus.textContent = `读取失败：${error.message}`;
    }
}

function markSettingsDirty() {
    state.settingsDirty = true;
    elements.saveSettingsButton.disabled = false;
    elements.settingsStatus.textContent = "参数尚未保存";
}

function activateSettingsTab(tabName, focus = false) {
    const tabs = Array.from(
        elements.settingsTabs.querySelectorAll("[data-settings-tab]"),
    );
    const target = tabs.some((tab) => tab.dataset.settingsTab === tabName)
        ? tabName
        : "filter";
    state.settingsTab = target;
    tabs.forEach((tab) => {
        const active = tab.dataset.settingsTab === target;
        tab.classList.toggle("active", active);
        tab.setAttribute("aria-selected", String(active));
        tab.tabIndex = 0;
        if (active && focus) tab.focus();
    });
    elements.settingsTabPanels.forEach((panel) => {
        const active = panel.dataset.settingsPanel === target;
        panel.classList.toggle("active", active);
        panel.hidden = !active;
    });
    try {
        window.localStorage.setItem("dashboard-settings-tab", target);
    } catch (error) {
        // Local storage can be unavailable in restricted browser contexts.
    }
}

function initializeSettingsTabs() {
    let initialTab = "filter";
    try {
        initialTab =
            window.localStorage.getItem("dashboard-settings-tab") || "filter";
    } catch (error) {
        initialTab = "filter";
    }
    activateSettingsTab(initialTab);
}

function currentSettingsPayload() {
    return {
        strategy_timeframe: elements.strategyTimeframeInput.value,
        entry_enabled: elements.entryEnabledInput.checked,
        max_open_trades: Number(elements.maxOpenTradesInput.value),
        lookback_days: Number(elements.lookbackDaysInput.value),
        min_change_20d: Number(elements.minChange20dInput.value),
        max_change_20d: Number(elements.maxChange20dInput.value),
        max_drawdown_to_gain_ratio_pct: Number(
            elements.maxDrawdownToGainRatioInput.value,
        ),
        use_4h_ma_filter: elements.strategyTimeframeInput.value === "4h",
        use_ma99_filter: elements.useMa99FilterInput.checked,
        ma7_reclaim_enabled: elements.ma7ReclaimEnabledInput.checked,
        ma7_reclaim_tolerance_pct: Number(
            elements.ma7ReclaimToleranceInput.value,
        ),
        ma7_reclaim_lookback_days: Number(
            elements.ma7ReclaimLookbackInput.value,
        ),
        ma7_exit_threshold_pct: Number(elements.ma7ExitThresholdInput.value),
        hard_stoploss_pct: Number(elements.hardStoplossInput.value),
        no_progress_exit_enabled: elements.noProgressExitEnabledInput.checked,
        candidate_reentry_required:
            elements.candidateReentryRequiredInput.checked,
        peak_drawdown_stop_enabled: elements.drawdownStopModeInput.value === "static",
        peak_drawdown_stop_pct: Number(elements.peakDrawdownStopInput.value),
        dynamic_drawdown_stop_enabled: elements.drawdownStopModeInput.value === "dynamic",
        dynamic_drawdown_activation_pct: Number(
            elements.dynamicDrawdownActivationInput.value,
        ),
        dynamic_max_profit_giveback_pct: Number(
            elements.dynamicMaxProfitGivebackInput.value,
        ),
        chandelier_exit_enabled: elements.chandelierExitEnabledInput.checked,
        partial_take_profit_enabled:
            elements.partialTakeProfitEnabledInput.checked,
        cooldown_enabled: elements.cooldownEnabledInput.checked,
        cooldown_hours: Number(elements.cooldownHoursInput.value),
        candidate_scan_interval_seconds:
            Math.round(Number(elements.scanIntervalInput.value) * 3600),
    };
}

function applyStrategyPreset(timeframe) {
    const preset = STRATEGY_PRESETS[timeframe] || STRATEGY_PRESETS["1d"];
    elements.maxOpenTradesInput.value = preset.max_open_trades ?? 1;
    elements.lookbackDaysInput.value = preset.lookback_days;
    elements.minChange20dInput.value = preset.min_change_20d;
    elements.maxChange20dInput.value = preset.max_change_20d;
    elements.maxDrawdownToGainRatioInput.value =
        preset.max_drawdown_to_gain_ratio_pct;
    elements.useMa99FilterInput.checked = preset.use_ma99_filter;
    elements.ma7ReclaimEnabledInput.checked = preset.ma7_reclaim_enabled;
    elements.ma7ReclaimToleranceInput.value =
        preset.ma7_reclaim_tolerance_pct;
    elements.ma7ReclaimLookbackInput.value =
        preset.ma7_reclaim_lookback_days;
    elements.ma7ExitThresholdInput.value = preset.ma7_exit_threshold_pct;
    elements.hardStoplossInput.value = preset.hard_stoploss_pct;
    elements.drawdownStopModeInput.value =
        preset.dynamic_drawdown_stop_enabled ? "dynamic" : "off";
    elements.dynamicDrawdownActivationInput.value =
        preset.dynamic_drawdown_activation_pct;
    elements.dynamicMaxProfitGivebackInput.value =
        preset.dynamic_max_profit_giveback_pct;
    elements.chandelierExitEnabledInput.checked =
        preset.chandelier_exit_enabled;
    elements.partialTakeProfitEnabledInput.checked =
        preset.partial_take_profit_enabled;
    elements.drawdownProtectionDetails.open =
        preset.dynamic_drawdown_stop_enabled;
    elements.advancedExitDetails.open =
        preset.chandelier_exit_enabled || preset.partial_take_profit_enabled;
    elements.cooldownEnabledInput.checked = preset.cooldown_enabled;
    elements.cooldownHoursInput.value = preset.cooldown_hours;
    elements.scanIntervalInput.value = preset.candidate_scan_interval_hours;
    updateFilterRuleValues();
    markSettingsDirty();
}

function updateFilterRuleValues() {
    const entryEnabled = elements.entryEnabledInput.checked;
    const lookbackDays = Number(elements.lookbackDaysInput.value);
    const minChange = Number(elements.minChange20dInput.value);
    const maxChange = Number(elements.maxChange20dInput.value);
    const maxDrawdownToGainRatio = Number(
        elements.maxDrawdownToGainRatioInput.value,
    );
    const ma7ExitThreshold = Number(elements.ma7ExitThresholdInput.value);
    const hardStoploss = Number(elements.hardStoplossInput.value);
    const noProgressExitEnabled = elements.noProgressExitEnabledInput.checked;
    const drawdownStopMode = elements.drawdownStopModeInput.value;
    const peakDrawdownStopEnabled = drawdownStopMode === "static";
    const peakDrawdownStop = Number(elements.peakDrawdownStopInput.value);
    const dynamicDrawdownStopEnabled = drawdownStopMode === "dynamic";
    elements.staticDrawdownSetting.classList.toggle(
        "is-hidden",
        drawdownStopMode !== "static",
    );
    elements.dynamicDrawdownSetting.classList.toggle(
        "is-hidden",
        drawdownStopMode !== "dynamic",
    );
    elements.dynamicProfitGivebackSetting.classList.toggle(
        "is-hidden",
        drawdownStopMode !== "dynamic",
    );
    const dynamicDrawdownActivation = Number(
        elements.dynamicDrawdownActivationInput.value,
    );
    const dynamicMaxProfitGiveback = Number(
        elements.dynamicMaxProfitGivebackInput.value,
    );
    const chandelierExitEnabled = elements.chandelierExitEnabledInput.checked;
    const partialTakeProfitEnabled =
        elements.partialTakeProfitEnabledInput.checked;
    const cooldownEnabled = elements.cooldownEnabledInput.checked;
    const candidateReentryRequired =
        elements.candidateReentryRequiredInput.checked;
    const cooldownHours = Number(elements.cooldownHoursInput.value);
    const maTimeframe =
        elements.strategyTimeframeInput.value === "4h" ? "4h" : "日K";
    elements.ma7ReclaimLookbackUnit.textContent =
        elements.strategyTimeframeInput.value === "4h" ? "根4h K线" : "日";
    const lookbackText = Number.isInteger(lookbackDays)
        ? `${lookbackDays}日`
        : "--日";
    elements.minChangeRule.textContent = Number.isFinite(minChange)
        ? `${minChange}%`
        : "--";
    elements.maxChangeRule.textContent = Number.isFinite(maxChange)
        ? `${maxChange}%`
        : "--";
    elements.maxDrawdownToGainRatioRule.textContent = Number.isFinite(
        maxDrawdownToGainRatio,
    )
        ? `${maxDrawdownToGainRatio}%`
        : "--";
    elements.maFilterRule.textContent = maTimeframe;
    elements.ma7ExitThresholdTimeframe.textContent = maTimeframe;
    elements.ma7ExitRuleTimeframe.textContent = maTimeframe;
    const useMa99Filter = elements.useMa99FilterInput.checked;
    elements.ma99FilterLabel.textContent = useMa99Filter ? "开启" : "关闭";
    elements.ma99RuleSuffix.hidden = !useMa99Filter;
    elements.ma99RuleSuffix.textContent = useMa99Filter
        ? " > MA99 且 MA99斜率 > 0"
        : "";
    elements.ma7ReclaimEnabledLabel.textContent =
        elements.ma7ReclaimEnabledInput.checked ? "开启" : "关闭";
    elements.lookbackDaysTexts.forEach((element) => {
        element.textContent = lookbackText;
    });
    elements.changePeriodLabel.textContent = `${lookbackText}涨幅`;
    elements.drawdownPeriodLabel.textContent = `${lookbackText}高点回撤`;
    elements.ma7ExitThresholdRule.textContent = Number.isFinite(ma7ExitThreshold)
        ? `${ma7ExitThreshold}%`
        : "--";
    elements.hardStoplossRule.textContent = Number.isFinite(hardStoploss)
        ? `${hardStoploss}%`
        : "--";
    elements.noProgressExitEnabledLabel.textContent =
        noProgressExitEnabled ? "开启" : "关闭";
    elements.noProgressExitRule.textContent =
        `持仓 12 小时最高盈利未达到 2% 时卖出（${
            noProgressExitEnabled ? "开启" : "关闭"
        }）`;
    elements.noProgressExitRule.classList.toggle(
        "is-hidden",
        !noProgressExitEnabled,
    );
    const peakDrawdownText = Number.isFinite(peakDrawdownStop)
        ? `${peakDrawdownStop}%`
        : "--";
    elements.peakDrawdownStopRule.textContent =
        `固定最高价回撤超过 ${peakDrawdownText} 时卖出（${
            peakDrawdownStopEnabled ? "开启" : "关闭"
        }）`;
    elements.dynamicDrawdownActivationRule.textContent = Number.isFinite(
        dynamicDrawdownActivation,
    )
        ? `${dynamicDrawdownActivation}%`
        : "--";
    elements.dynamicDrawdownStopRule.textContent =
        `动态止损：盈利达到 ${Number.isFinite(dynamicDrawdownActivation)
            ? `${dynamicDrawdownActivation}%`
            : "--"} 后启动，锁定最高盈利的 50%，最多回吐 ${
            Number.isFinite(dynamicMaxProfitGiveback)
                ? `${dynamicMaxProfitGiveback}%`
                : "--"
        }（${
            dynamicDrawdownStopEnabled ? "开启" : "关闭"
        }）`;
    elements.peakDrawdownStopRule.classList.toggle(
        "is-hidden",
        !peakDrawdownStopEnabled,
    );
    elements.dynamicDrawdownStopRule.classList.toggle(
        "is-hidden",
        !dynamicDrawdownStopEnabled,
    );
    elements.chandelierExitEnabledLabel.textContent =
        chandelierExitEnabled ? "开启" : "关闭";
    elements.chandelierExitRule.textContent =
        `吊灯止损：最高价 - ATR(22) × 3（${
            chandelierExitEnabled ? "开启" : "关闭"
        }）`;
    elements.partialTakeProfitEnabledLabel.textContent =
        partialTakeProfitEnabled ? "开启" : "关闭";
    elements.partialTakeProfitRule.textContent =
        `分批止盈：盈利15%减半仓，剩余仓位回撤5%卖出（${
            partialTakeProfitEnabled ? "开启" : "关闭"
        }）`;
    elements.cooldownEnabledLabel.textContent = cooldownEnabled ? "开启" : "关闭";
    elements.cooldownDurationSetting.classList.toggle(
        "is-hidden",
        !cooldownEnabled,
    );
    elements.cooldownRuleValue.textContent = Number.isFinite(cooldownHours)
        ? `${cooldownHours}小时`
        : "--";
    elements.cooldownRule.textContent =
        `卖出后同一币种冷却 ${Number.isFinite(cooldownHours)
            ? `${cooldownHours}小时`
            : "--"}（${cooldownEnabled ? "开启" : "关闭"}）`;
    elements.cooldownRule.classList.toggle("is-hidden", !cooldownEnabled);
    elements.candidateReentryRequiredLabel.textContent =
        candidateReentryRequired ? "开启" : "关闭";
    elements.candidateReentryRequiredRule.textContent =
        `卖出后需退出候选池并重新入选才允许买入（${
            candidateReentryRequired ? "开启" : "关闭"
        }）`;
    elements.candidateReentryRequiredRule.classList.toggle(
        "is-hidden",
        !candidateReentryRequired,
    );
    elements.cooldownRuleSection.classList.toggle(
        "is-hidden",
        !cooldownEnabled && !candidateReentryRequired,
    );
    elements.entryEnabledLabel.textContent = entryEnabled ? "允许" : "禁止";
    elements.entryEnabledBadge.textContent = entryEnabled ? "允许买入" : "仅允许卖出";
    elements.entryEnabledRule.textContent = entryEnabled
        ? "进入候选列表后直接买入"
        : "禁止所有新买入，现有持仓继续执行卖出";
}

async function saveSettings() {
    const lookbackDays = Number(elements.lookbackDaysInput.value);
    const minChange20d = Number(elements.minChange20dInput.value);
    const maxChange20d = Number(elements.maxChange20dInput.value);
    const maxDrawdownToGainRatio = Number(
        elements.maxDrawdownToGainRatioInput.value,
    );
    const ma7ExitThreshold = Number(elements.ma7ExitThresholdInput.value);
    const hardStoploss = Number(elements.hardStoplossInput.value);
    const ma7ReclaimTolerance = Number(
        elements.ma7ReclaimToleranceInput.value,
    );
    const ma7ReclaimLookback = Number(
        elements.ma7ReclaimLookbackInput.value,
    );
    const peakDrawdownStop = Number(elements.peakDrawdownStopInput.value);
    const dynamicDrawdownActivation = Number(
        elements.dynamicDrawdownActivationInput.value,
    );
    const dynamicMaxProfitGiveback = Number(
        elements.dynamicMaxProfitGivebackInput.value,
    );
    const cooldownHours = Number(elements.cooldownHoursInput.value);
    if (!Number.isInteger(lookbackDays) || lookbackDays < 2 || lookbackDays > 364) {
        elements.settingsStatus.textContent = "统计周期请输入 2～364 的整数";
        return;
    }
    if (!Number.isFinite(minChange20d) || minChange20d < -100 || minChange20d > 10000) {
        elements.settingsStatus.textContent = "涨幅请输入 -100～10000";
        return;
    }
    if (!Number.isFinite(maxChange20d) || maxChange20d < -100 || maxChange20d > 10000) {
        elements.settingsStatus.textContent = "最大涨幅请输入 -100～10000";
        return;
    }
    if (maxChange20d <= minChange20d) {
        elements.settingsStatus.textContent = "最大涨幅必须大于最小涨幅";
        return;
    }
    if (
        !Number.isFinite(maxDrawdownToGainRatio)
        || maxDrawdownToGainRatio < 0
        || maxDrawdownToGainRatio > 100
    ) {
        elements.settingsStatus.textContent = "回撤/涨幅比例请输入 0～100";
        return;
    }
    if (
        !Number.isFinite(ma7ExitThreshold)
        || ma7ExitThreshold < 0
        || ma7ExitThreshold > 100
    ) {
        elements.settingsStatus.textContent = "MA7卖出阈值请输入 0～100";
        return;
    }
    if (!Number.isFinite(hardStoploss) || hardStoploss < 0.1 || hardStoploss > 99) {
        elements.settingsStatus.textContent = "硬止损请输入 0.1～99";
        return;
    }
    if (
        !Number.isFinite(ma7ReclaimTolerance)
        || ma7ReclaimTolerance < 0.1
        || ma7ReclaimTolerance > 5
    ) {
        elements.settingsStatus.textContent = "MA7 回踩容差请输入 0.1～5";
        return;
    }
    if (
        !Number.isInteger(ma7ReclaimLookback)
        || ma7ReclaimLookback < 1
        || ma7ReclaimLookback > 5
    ) {
        elements.settingsStatus.textContent = "回捞窗口请输入 1～5 根K线整数";
        return;
    }
    if (
        !Number.isFinite(peakDrawdownStop)
        || peakDrawdownStop < 0.1
        || peakDrawdownStop > 99
    ) {
        elements.settingsStatus.textContent = "最高点回撤比例请输入 0.1～99";
        return;
    }
    if (
        !Number.isFinite(dynamicDrawdownActivation)
        || dynamicDrawdownActivation < 0.1
        || dynamicDrawdownActivation > 100
    ) {
        elements.settingsStatus.textContent = "动态止损启动盈利请输入 0.1～100";
        return;
    }
    if (
        !Number.isFinite(dynamicMaxProfitGiveback)
        || dynamicMaxProfitGiveback < 0.5
        || dynamicMaxProfitGiveback > 50
    ) {
        elements.settingsStatus.textContent = "最大盈利回吐请输入 0.5～50";
        return;
    }
    if (
        !Number.isFinite(cooldownHours)
        || cooldownHours < 0.1
        || cooldownHours > 168
    ) {
        elements.settingsStatus.textContent = "冷却时长请输入 0.1～168 小时";
        return;
    }
    const scanIntervalHours = Number(elements.scanIntervalInput.value);
    if (
        !Number.isFinite(scanIntervalHours)
        || scanIntervalHours < 0.1
        || scanIntervalHours > 24
    ) {
        elements.settingsStatus.textContent = "扫描频率请输入 0.1～24 小时";
        return;
    }
    const maxOpenTrades = Number(elements.maxOpenTradesInput.value);
    if (!Number.isInteger(maxOpenTrades) || maxOpenTrades < 1 || maxOpenTrades > 4) {
        elements.settingsStatus.textContent = "最大持仓数量请输入 1～4 的整数";
        return;
    }

    elements.settingsStatus.textContent = "保存中...";
    elements.saveSettingsButton.disabled = true;
    try {
        const result = await fetchJson("/api/settings", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(currentSettingsPayload()),
        });
        updateFilterRuleValues();
        state.settingsDirty = false;
        elements.settingsStatus.textContent = result.preview_promoted
            ? "参数已保存，预览候选已正式用于交易"
            : "参数已保存；筛选条件将在扫描后生效";
        await loadCandidates({ reloadChart: true });
    } catch (error) {
        elements.settingsStatus.textContent = `保存失败：${error.message}`;
        elements.saveSettingsButton.disabled = false;
    }
}

function formatBacktestDate(value) {
    if (!value) return "--";
    return new Intl.DateTimeFormat("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
    }).format(new Date(value));
}

function formatBacktestPrice(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) {
        return "--";
    }
    return formatPrice(value);
}

function formatBacktestAmount(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) {
        return "--";
    }
    return `${Number(value).toFixed(2)} USDT`;
}

function formatBacktestMonth(value) {
    if (!value) return "--";
    return new Intl.DateTimeFormat("zh-CN", {
        year: "numeric",
        month: "2-digit",
        timeZone: "UTC",
    }).format(new Date(value));
}

function calculateMonthlyReturns(curve) {
    const points = (curve || [])
        .map((point) => ({
            time: point.time,
            date: new Date(point.time),
            equity: Number(point.equity),
        }))
        .filter(
            (point) => point.time
                && !Number.isNaN(point.date.getTime())
                && Number.isFinite(point.equity),
        )
        .sort((left, right) => left.date - right.date);
    const months = [];
    let current = null;
    let previousPoint = null;

    points.forEach((point) => {
        const key = `${point.date.getUTCFullYear()}-${String(
            point.date.getUTCMonth() + 1,
        ).padStart(2, "0")}`;
        if (!current || current.key !== key) {
            if (current) months.push(current);
            current = {
                key,
                label: formatBacktestMonth(point.time),
                startEquity: previousPoint?.equity ?? point.equity,
                endEquity: point.equity,
            };
        }
        current.endEquity = point.equity;
        previousPoint = point;
    });
    if (current) months.push(current);

    return months.map((month) => ({
        ...month,
        returnPct: month.startEquity
            ? (month.endEquity / month.startEquity - 1) * 100
            : 0,
    }));
}

function renderMonthlyReturns(curve) {
    const rows = calculateMonthlyReturns(curve);
    elements.monthlyReturnsBody.innerHTML = "";
    if (!rows.length) {
        const row = document.createElement("tr");
        const cell = document.createElement("td");
        cell.colSpan = 4;
        cell.textContent = "暂无月度收益数据";
        row.append(cell);
        elements.monthlyReturnsBody.append(row);
        return;
    }

    const fragment = document.createDocumentFragment();
    rows.forEach((month) => {
        const row = document.createElement("tr");
        const values = [
            month.label,
            formatBacktestAmount(month.startEquity),
            formatBacktestAmount(month.endEquity),
            `${month.returnPct >= 0 ? "+" : ""}${month.returnPct.toFixed(2)}%`,
        ];
        values.forEach((value, index) => {
            const cell = document.createElement("td");
            cell.textContent = value;
            if (index === 3) {
                cell.className = month.returnPct >= 0 ? "positive" : "negative";
            }
            row.append(cell);
        });
        fragment.append(row);
    });
    elements.monthlyReturnsBody.append(fragment);
}

function backtestExitReason(reason) {
    const reasons = {
        dynamic_peak_drawdown: "动态锁盈",
        no_progress_12h: "无进展",
        stop_loss: "止损",
        break_even_stop: "保本止损",
        peak_drawdown: "最高点回撤",
        take_profit: "固定止盈",
        ema20_exit: "EMA20",
        time_exit: "最长持仓",
        ma7_exit: "MA7",
        "1d_ma7_buffer_break": "日K MA7",
        "4h_ma7_buffer_break": "4h MA7",
        chandelier_exit: "吊灯止损",
        trailing_stop_loss: "吊灯止损（动态止损触发）",
        partial_trailing_stop: "分批止盈后移动止损",
        end_of_backtest: "区间结束",
    };
    return reasons[reason] || reason || "--";
}

function appendBacktestTradeRows() {
    const trades = state.backtestTrades;
    const start = state.backtestVisibleTradeCount;
    const end = Math.min(start + BACKTEST_TRADE_PAGE_SIZE, trades.length);
    if (start >= end) return;
    const fragment = document.createDocumentFragment();
    trades.slice(start, end).forEach((trade) => {
        const row = document.createElement("tr");
        const entryAmount = trade.entry_amount ?? trade.stake;
        const exitAmount = trade.exit_amount
            ?? (
                Number.isFinite(Number(trade.profit_abs))
                    ? Number(entryAmount) + Number(trade.profit_abs)
                    : null
            );
        const exitReason = trade.partial_take_profit
            ? `15%减半仓 · ${backtestExitReason(trade.exit_reason)}`
            : backtestExitReason(trade.exit_reason);
        const values = [
            trade.pair,
            formatBacktestDate(trade.entry_time),
            formatBacktestPrice(trade.entry_price),
            formatBacktestAmount(entryAmount),
            formatBacktestDate(trade.exit_time),
            formatBacktestPrice(trade.exit_price),
            formatBacktestAmount(exitAmount),
            `${trade.profit_pct >= 0 ? "+" : ""}${trade.profit_pct.toFixed(2)}%`,
            exitReason,
        ];
        values.forEach((value, index) => {
            const cell = document.createElement("td");
            cell.textContent = value;
            if (index === 7) {
                cell.className = trade.profit_pct >= 0 ? "positive" : "negative";
            }
            row.append(cell);
        });
        fragment.append(row);
    });
    elements.backtestTradesBody.append(fragment);
    state.backtestVisibleTradeCount = end;
    const candidateCount =
        state.backtestResult?.summary?.candidate_pair_count || 0;
    elements.backtestTradeMeta.textContent =
        `已加载 ${end}/${trades.length} 笔 · ${candidateCount} 个候选`;
}

function renderBacktestTrades(trades) {
    state.backtestTrades = trades || [];
    state.backtestVisibleTradeCount = 0;
    elements.backtestTradesBody.innerHTML = "";
    elements.backtestTableWrap.scrollTop = 0;
    if (!state.backtestTrades.length) {
        const row = document.createElement("tr");
        const cell = document.createElement("td");
        cell.colSpan = 9;
        cell.textContent = "回测区间内没有完成交易";
        row.append(cell);
        elements.backtestTradesBody.append(row);
        elements.backtestTradeMeta.textContent = "0 笔交易";
        return;
    }
    appendBacktestTradeRows();
}

function loadMoreBacktestTrades() {
    const container = elements.backtestTableWrap;
    if (
        state.backtestVisibleTradeCount < state.backtestTrades.length
        && container.scrollTop + container.clientHeight
            >= container.scrollHeight - 80
    ) {
        appendBacktestTradeRows();
    }
}

function maxDrawdownSegment(curve) {
    let peakIndex = 0;
    let peakEquity = Number(curve[0]?.equity);
    let maxDrawdown = 0;
    let segment = null;

    curve.forEach((point, index) => {
        const equity = Number(point.equity);
        if (!Number.isFinite(equity)) return;
        if (!Number.isFinite(peakEquity) || equity > peakEquity) {
            peakEquity = equity;
            peakIndex = index;
            return;
        }
        const drawdown = peakEquity ? (peakEquity - equity) / peakEquity : 0;
        if (drawdown > maxDrawdown) {
            maxDrawdown = drawdown;
            segment = { peakIndex, troughIndex: index };
        }
    });
    return segment;
}

function backtestEquityGeometry(rect, curve) {
    const left = 54;
    const right = 16;
    const top = 18;
    const bottom = 30;
    const width = Math.max(1, rect.width - left - right);
    const height = Math.max(1, rect.height - top - bottom);
    const values = curve.map((point) => Number(point.equity));
    let minValue = Math.min(...values);
    let maxValue = Math.max(...values);
    const padding = Math.max((maxValue - minValue) * 0.12, maxValue * 0.002, 1);
    minValue -= padding;
    maxValue += padding;
    const valueRange = maxValue - minValue;
    return {
        left,
        top,
        width,
        height,
        values,
        minValue,
        maxValue,
        valueRange,
        xForIndex: (index) => left + (index / (curve.length - 1)) * width,
        yForValue: (value) => top + ((maxValue - value) / valueRange) * height,
    };
}

function drawBacktestEquity() {
    const canvas = elements.backtestEquityCanvas;
    const curve = state.backtestResult?.equity_curve || [];
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const ratio = Math.max(1, window.devicePixelRatio || 1);
    canvas.width = Math.round(rect.width * ratio);
    canvas.height = Math.round(rect.height * ratio);
    const context = canvas.getContext("2d");
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, rect.width, rect.height);
    if (curve.length < 2) return;
    const {
        left,
        top,
        width,
        height,
        values,
        maxValue,
        valueRange,
        xForIndex,
        yForValue,
    } = backtestEquityGeometry(rect, curve);

    context.strokeStyle = COLORS.grid;
    context.fillStyle = COLORS.axis;
    context.font = "9px Inter, sans-serif";
    context.lineWidth = 1;
    for (let index = 0; index <= 4; index += 1) {
        const y = top + (height * index) / 4;
        context.beginPath();
        context.moveTo(left, y);
        context.lineTo(left + width, y);
        context.stroke();
        const label = maxValue - (valueRange * index) / 4;
        context.fillText(label.toFixed(0), 8, y + 3);
    }

    const lineColor = values.at(-1) >= values[0] ? COLORS.up : COLORS.down;
    context.strokeStyle = lineColor;
    context.lineWidth = 2;
    context.beginPath();
    curve.forEach((point, index) => {
        const x = xForIndex(index);
        const y = yForValue(Number(point.equity));
        if (index === 0) context.moveTo(x, y);
        else context.lineTo(x, y);
    });
    context.stroke();

    context.fillStyle = `${lineColor}18`;
    context.lineTo(left + width, top + height);
    context.lineTo(left, top + height);
    context.closePath();
    context.fill();

    const drawdownSegment = maxDrawdownSegment(curve);
    if (drawdownSegment && drawdownSegment.troughIndex > drawdownSegment.peakIndex) {
        context.save();
        context.strokeStyle = COLORS.down;
        context.lineWidth = 3;
        context.beginPath();
        for (
            let index = drawdownSegment.peakIndex;
            index <= drawdownSegment.troughIndex;
            index += 1
        ) {
            const x = xForIndex(index);
            const y = yForValue(Number(curve[index].equity));
            if (index === drawdownSegment.peakIndex) context.moveTo(x, y);
            else context.lineTo(x, y);
        }
        context.stroke();
        [drawdownSegment.peakIndex, drawdownSegment.troughIndex].forEach((index) => {
            context.beginPath();
            context.arc(
                xForIndex(index),
                yForValue(Number(curve[index].equity)),
                4,
                0,
                Math.PI * 2,
            );
            context.fillStyle = "#ffffff";
            context.fill();
            context.stroke();
        });
        context.restore();
    }

    if (
        Number.isInteger(state.backtestHoverIndex)
        && state.backtestHoverIndex >= 0
        && state.backtestHoverIndex < curve.length
    ) {
        const hoverPoint = curve[state.backtestHoverIndex];
        const hoverX = xForIndex(state.backtestHoverIndex);
        const hoverY = yForValue(Number(hoverPoint.equity));
        context.save();
        context.strokeStyle = COLORS.crosshair;
        context.lineWidth = 1;
        context.setLineDash([4, 4]);
        context.beginPath();
        context.moveTo(hoverX, top);
        context.lineTo(hoverX, top + height);
        context.stroke();
        context.setLineDash([]);
        context.fillStyle = "#ffffff";
        context.strokeStyle = lineColor;
        context.lineWidth = 2;
        context.beginPath();
        context.arc(hoverX, hoverY, 4, 0, Math.PI * 2);
        context.fill();
        context.stroke();
        context.restore();
    }

    context.fillStyle = COLORS.axis;
    context.textAlign = "left";
    context.fillText(formatBacktestDate(curve[0].time), left, rect.height - 9);
    context.textAlign = "right";
    context.fillText(
        formatBacktestDate(curve.at(-1).time),
        left + width,
        rect.height - 9,
    );
    context.textAlign = "left";
}

function updateBacktestEquityHover(clientX) {
    const curve = state.backtestResult?.equity_curve || [];
    if (curve.length < 2) return;
    const canvas = elements.backtestEquityCanvas;
    const rect = canvas.getBoundingClientRect();
    const geometry = backtestEquityGeometry(rect, curve);
    const relativeX = Math.max(
        0,
        Math.min(geometry.width, clientX - rect.left - geometry.left),
    );
    const index = Math.round(
        (relativeX / geometry.width) * (curve.length - 1),
    );
    const point = curve[index];
    const equity = Number(point.equity);
    const initialBalance = Number(
        state.backtestResult?.summary?.initial_balance || 0,
    );
    const profitPct = initialBalance
        ? (equity / initialBalance - 1) * 100
        : 0;
    const pointX = geometry.xForIndex(index);
    const pointY = geometry.yForValue(equity);
    const tooltip = elements.backtestEquityTooltip;

    state.backtestHoverIndex = index;
    tooltip.innerHTML = "";
    const value = document.createElement("strong");
    value.textContent = `${equity.toFixed(2)} USDT`;
    const time = document.createElement("span");
    time.textContent = formatBacktestDate(point.time);
    const profit = document.createElement("b");
    profit.className = profitPct >= 0 ? "positive" : "negative";
    profit.textContent = `${profitPct >= 0 ? "+" : ""}${profitPct.toFixed(2)}%`;
    tooltip.append(value, time, profit);
    tooltip.style.display = "block";

    const tooltipWidth = 142;
    const tooltipHeight = 58;
    tooltip.style.left = `${Math.max(
        8,
        Math.min(rect.width - tooltipWidth - 8, pointX + 10),
    )}px`;
    tooltip.style.top = `${Math.max(
        8,
        Math.min(rect.height - tooltipHeight - 8, pointY - tooltipHeight / 2),
    )}px`;
    drawBacktestEquity();
}

function clearBacktestEquityHover() {
    state.backtestHoverIndex = null;
    elements.backtestEquityTooltip.style.display = "none";
    drawBacktestEquity();
}

function renderBacktestResult(result) {
    if (!result?.summary) return;
    const previousResultId = state.backtestResult?.meta?.generated_at;
    const resultId = result.meta?.generated_at;
    const resultChanged = previousResultId !== resultId;
    state.backtestResult = result;
    state.backtestHoverIndex = null;
    elements.backtestEquityTooltip.style.display = "none";
    const summary = result.summary;
    const profit = Number(summary.total_profit_pct);
    elements.backtestTotalProfit.textContent =
        `${profit >= 0 ? "+" : ""}${profit.toFixed(2)}%`;
    elements.backtestTotalProfit.className = profit >= 0 ? "positive" : "negative";
    elements.backtestEndingBalance.textContent =
        `${Number(summary.ending_balance).toFixed(2)} USDT`;
    elements.backtestMaxDrawdown.textContent =
        `${Number(summary.max_drawdown_pct).toFixed(2)}%`;
    elements.backtestMaxDrawdown.className =
        Number(summary.max_drawdown_pct) > 0 ? "negative" : "";
    elements.backtestWinRate.textContent =
        `${Number(summary.win_rate_pct).toFixed(2)}%`;
    elements.backtestTradeCount.textContent = String(summary.trade_count);
    elements.backtestProfitFactor.textContent =
        summary.profit_factor === null ? "--" : Number(summary.profit_factor).toFixed(2);
    elements.backtestDateRange.textContent =
        `${formatBacktestDate(summary.start)} 至 ${formatBacktestDate(summary.end)}`;
    elements.backtestStatusMeta.textContent =
        result.meta.execution_timeframe === "4h"
            ? `${result.meta.days || "--"} 天 · 4h策略 · 4h成交`
            : `${result.meta.days || "--"} 天 · 30 分钟选股 · 15 分钟成交`;
    elements.backtestUniverseMeta.textContent =
        `${result.meta.processed_pairs}/${result.meta.universe_size} 个交易对 · ${
            result.meta.scan_timeframe
        } 选股`;
    elements.backtestEmpty.style.display =
        result.equity_curve?.length > 1 ? "none" : "grid";
    renderMonthlyReturns(result.equity_curve || []);
    if (resultChanged) {
        renderBacktestTrades(result.trades || []);
    }
    requestAnimationFrame(drawBacktestEquity);
}

function applyBacktestStatus(status) {
    const progress = Number(status.progress || 0);
    elements.backtestStatusText.textContent = status.message || "尚未运行回测";
    elements.backtestProgressBar.style.width = `${Math.max(0, Math.min(100, progress))}%`;
    elements.backtestProgressText.textContent = `${progress.toFixed(0)}%`;
    elements.runBacktestButton.disabled = Boolean(status.running);
    elements.runBacktestButton.textContent = status.running ? "回测运行中" : "运行回测";
    elements.cancelBacktestButton.classList.toggle(
        "is-hidden",
        !status.running,
    );
    if (status.result) renderBacktestResult(status.result);
}

async function loadBacktestStatus() {
    try {
        const status = await fetchJson("/api/backtest/status");
        applyBacktestStatus(status);
        if (status.running && !state.backtestPollTimer) {
            state.backtestPollTimer = window.setInterval(loadBacktestStatus, 2000);
        }
        if (!status.running && state.backtestPollTimer) {
            window.clearInterval(state.backtestPollTimer);
            state.backtestPollTimer = null;
        }
    } catch (error) {
        elements.backtestStatusText.textContent = `读取失败：${error.message}`;
        elements.runBacktestButton.disabled = false;
    }
}

async function startBacktest() {
    const days = Number(elements.backtestDaysInput.value);
    const initialBalance = Number(elements.backtestInitialBalanceInput.value);
    if (!Number.isInteger(days) || days < 7 || days > 540) {
        elements.backtestStatusText.textContent = "回测区间必须为 7～540 天";
        return;
    }
    if (
        !Number.isFinite(initialBalance)
        || initialBalance < 100
        || initialBalance > 10000000
    ) {
        elements.backtestStatusText.textContent = "初始资金请输入 100～10000000";
        return;
    }
    elements.runBacktestButton.disabled = true;
    elements.backtestStatusText.textContent = "正在启动回测...";
    const strategyTimeframe = elements.strategyTimeframeInput.value;
    elements.backtestStatusMeta.textContent =
        strategyTimeframe === "4h"
            ? `${days} 天 · 4h策略 · 4h成交`
            : `${days} 天 · 30 分钟选股 · 15 分钟成交`;
    try {
        const status = await fetchJson("/api/backtest", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                days,
                initial_balance: initialBalance,
                strategy_timeframe: strategyTimeframe,
            }),
        });
        applyBacktestStatus(status);
        if (!state.backtestPollTimer) {
            state.backtestPollTimer = window.setInterval(loadBacktestStatus, 2000);
        }
    } catch (error) {
        elements.backtestStatusText.textContent = `启动失败：${error.message}`;
        elements.runBacktestButton.disabled = false;
    }
}

async function cancelBacktest() {
    elements.cancelBacktestButton.disabled = true;
    try {
        const status = await fetchJson("/api/backtest", {
            method: "DELETE",
        });
        applyBacktestStatus(status);
        if (state.backtestPollTimer) {
            window.clearInterval(state.backtestPollTimer);
            state.backtestPollTimer = null;
        }
    } catch (error) {
        elements.backtestStatusText.textContent = `停止失败：${error.message}`;
    } finally {
        elements.cancelBacktestButton.disabled = false;
    }
}

function filterCandidates() {
    const keyword = elements.searchInput.value.trim().toUpperCase();
    state.filteredCandidates = state.candidates.filter((candidate) =>
        candidate.pair.toUpperCase().includes(keyword),
    );
    sortCandidates(state.filteredCandidates);
    renderCandidateList();
}

// Sort keys map to a candidate field plus a direction. Missing values (null)
// always sink to the bottom regardless of direction so incomplete rows never
// crowd out the meaningful ones. "default" keeps the backend rank order.
const SORT_OPTIONS = {
    change_desc: { field: "change_20d", direction: -1 },
    change_asc: { field: "change_20d", direction: 1 },
    drawdown_desc: { field: "drawdown_20d", direction: -1 },
    drawdown_asc: { field: "drawdown_20d", direction: 1 },
};

function sortCandidates(candidates) {
    const option = SORT_OPTIONS[state.sortKey];
    if (!option) {
        candidates.sort((a, b) => a.rank - b.rank);
        return;
    }
    candidates.sort((a, b) => {
        const valueA = a[option.field];
        const valueB = b[option.field];
        const missingA = valueA === null || valueA === undefined;
        const missingB = valueB === null || valueB === undefined;
        if (missingA && missingB) return a.rank - b.rank;
        if (missingA) return 1;
        if (missingB) return -1;
        if (valueA === valueB) return a.rank - b.rank;
        return (valueA - valueB) * option.direction;
    });
}

// The row subline surfaces whatever metric the list is sorted by, so the
// ordering is legible at a glance. Falls back to 24h volume for the default
// sort.
function candidateSubline(candidate) {
    const option = SORT_OPTIONS[state.sortKey];
    if (option && option.field === "change_20d") {
        const value = candidate.change_20d;
        return value === null || value === undefined
            ? "涨幅 --"
            : `${value >= 0 ? "+" : ""}${value.toFixed(2)}% 涨幅`;
    }
    if (option && option.field === "drawdown_20d") {
        const value = candidate.drawdown_20d;
        return value === null || value === undefined
            ? "回撤 --"
            : `-${value.toFixed(2)}% 回撤`;
    }
    return `24h ${formatQuoteVolume(candidate.quote_volume_24h)} USDT`;
}

function renderCandidateList() {
    elements.candidateList.innerHTML = "";
    if (!state.filteredCandidates.length) {
        const empty = document.createElement("div");
        empty.className = "empty-state";
        empty.textContent = state.candidates.length ? "没有匹配的候选币" : "当前没有候选币";
        elements.candidateList.append(empty);
        return;
    }

    const fragment = document.createDocumentFragment();
    state.filteredCandidates.forEach((candidate) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `candidate-item${candidate.pair === state.selectedPair ? " active" : ""}`;
        button.dataset.pair = candidate.pair;
        button.setAttribute("aria-label", `查看 ${candidate.pair} K线`);

        const rank = document.createElement("span");
        rank.className = "rank";
        rank.textContent = String(candidate.rank).padStart(2, "0");

        const identity = document.createElement("span");
        const symbol = document.createElement("span");
        symbol.className = "pair-symbol";
        symbol.textContent = candidate.pair;
        const volume = document.createElement("span");
        volume.className = "pair-volume";
        volume.textContent = candidateSubline(candidate);
        identity.append(symbol, volume);

        const price = document.createElement("span");
        price.className = "candidate-price";
        price.textContent = formatPrice(candidate.price);

        button.append(rank, identity, price);
        button.addEventListener("click", async () => {
            if (state.selectedPair === candidate.pair) return;
            state.selectedPair = candidate.pair;
            renderCandidateList();
            updateCandidateMetrics();
            await loadChart({ resetView: true });
        });
        fragment.append(button);
    });
    elements.candidateList.append(fragment);
}

function updateCandidateMetrics() {
    const candidate = selectedCandidate();
    if (!candidate) return;

    elements.pairName.textContent = candidate.pair;
    elements.provisionalBadge.hidden = !candidate.is_provisional_daily_candle;
    const change20d = candidate.change_20d;
    elements.change20dValue.textContent = change20d === null
        ? "--"
        : `${change20d >= 0 ? "+" : ""}${change20d.toFixed(2)}%`;
    elements.change20dValue.className = change20d !== null && change20d >= 0
        ? "positive"
        : "negative";
    const drawdown20d = candidate.drawdown_20d;
    elements.drawdown20dValue.textContent = drawdown20d === null
        ? "--"
        : `-${drawdown20d.toFixed(2)}%`;
    elements.volume24h.textContent = `${formatQuoteVolume(candidate.quote_volume_24h)} USDT`;
}

async function loadChart({ resetView = false, forceRefresh = false } = {}) {
    const pair = state.selectedPair;
    if (!pair) return;

    const cacheKey = `${pair}|${state.timeframe}`;
    const cached = state.candleCache.get(cacheKey);
    const now = Date.now();

    // Instant paint from cache so switching timeframe / re-selecting a pair
    // feels immediate. If the cache is still fresh we skip the network round
    // trip entirely; otherwise we repaint from cache first and revalidate in
    // the background without showing the loading state.
    if (cached && !forceRefresh) {
        applyCandlePayload(cached.payload, { resetView });
        if (now - cached.ts < CANDLE_CACHE_TTL_MS) {
            return true;
        }
    }

    const requestId = ++state.chartRequest;
    const showLoading = !cached || forceRefresh;
    if (showLoading) {
        elements.chartLoading.textContent = "正在加载 K 线...";
        elements.chartLoading.classList.remove("hidden");
        elements.chartTooltip.style.display = "none";
    }

    try {
        const params = new URLSearchParams({
            pair,
            timeframe: state.timeframe,
            limit: "365",
        });
        if (forceRefresh) params.set("refresh", "true");
        const payload = await fetchJson(`/api/candles?${params}`);
        if (requestId !== state.chartRequest) return;
        state.candleCache.set(cacheKey, { payload, ts: Date.now() });
        // A background revalidation must not reset the user's zoom/pan; only the
        // first paint (no prior cache) honors resetView here.
        applyCandlePayload(payload, { resetView: resetView && !cached });
        elements.chartLoading.classList.add("hidden");
        return true;
    } catch (error) {
        if (requestId !== state.chartRequest) return;
        if (cached) {
            // Keep the cached chart on screen; a transient refresh failure
            // should not blank out a usable view.
            return false;
        }
        elements.chartLoading.textContent = `K 线加载失败：${error.message}`;
        elements.chartLoading.classList.remove("hidden");
        return false;
    }
}

function applyCandlePayload(payload, { resetView = false } = {}) {
    state.candles = payload.candles || [];
    state.hoverIndex = null;
    if (resetView || state.viewCount === null) {
        resetChartView();
    } else {
        state.viewCount = Math.min(state.viewCount, state.candles.length);
        state.viewEnd = state.candles.length;
    }
    updateChartMetrics();
    drawChart();
}

function updateChartMetrics() {
    const candles = state.candles;
    const latest = candles.at(-1);
    if (!latest) return;

    elements.latestPrice.textContent = formatPrice(latest.close);
    elements.ma7Value.textContent = formatPrice(latest.ma7);
    elements.ma20Value.textContent = formatPrice(latest.ma20);
    elements.ma99Value.textContent = formatPrice(latest.ma99);

    const change = latest.changeToday;
    elements.priceChange.textContent = change === null
        ? "--"
        : `${change >= 0 ? "+" : ""}${change.toFixed(2)}%`;
    elements.priceChange.className = change !== null && change >= 0
        ? "positive"
        : "negative";
}

function chartGeometry() {
    const canvas = elements.chartCanvas;
    const rect = canvas.getBoundingClientRect();
    const ratio = Math.max(1, window.devicePixelRatio || 1);
    const width = Math.max(300, rect.width);
    const height = Math.max(300, rect.height);
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    const context = canvas.getContext("2d");
    context.setTransform(ratio, 0, 0, ratio, 0, 0);

    const left = 12;
    const right = 76;
    const top = 34;
    const bottom = 25;
    const volumeHeight = Math.max(65, height * 0.18);
    const priceBottom = height - bottom - volumeHeight - 12;
    return {
        context,
        width,
        height,
        left,
        right,
        top,
        bottom,
        volumeHeight,
        priceBottom,
        plotWidth: width - left - right,
        volumeTop: priceBottom + 12,
        volumeBottom: height - bottom,
    };
}

function visibleCandles(geometry) {
    const defaultVisible = Math.max(120, Math.floor(geometry.plotWidth / 3.2));
    const count = Math.min(
        state.viewCount || defaultVisible,
        state.candles.length,
    );
    const end = Math.min(
        state.viewEnd || state.candles.length,
        state.candles.length,
    );
    const start = Math.max(0, end - count);
    return state.candles.slice(start, end);
}

function resetChartView() {
    state.viewCount = Math.min(180, state.candles.length);
    state.viewEnd = state.candles.length;
    state.hoverIndex = null;
    state.trackpadPanRemainder = 0;
    elements.chartTooltip.style.display = "none";
}

function drawChart() {
    const geometry = chartGeometry();
    const { context, width, height } = geometry;
    context.clearRect(0, 0, width, height);

    const candles = visibleCandles(geometry);
    if (!candles.length) return;

    const priceValues = [];
    candles.forEach((candle) => {
        priceValues.push(candle.low, candle.high);
        ["ma7", "ma20", "ma99"].forEach((key) => {
            if (candle[key] !== null) priceValues.push(candle[key]);
        });
    });
    let minPrice = Math.min(...priceValues);
    let maxPrice = Math.max(...priceValues);
    const padding = Math.max((maxPrice - minPrice) * 0.07, maxPrice * 0.0005);
    minPrice = Math.max(0, minPrice - padding);
    maxPrice += padding;

    const yForPrice = (price) =>
        geometry.top
        + ((maxPrice - price) / (maxPrice - minPrice))
        * (geometry.priceBottom - geometry.top);
    const step = geometry.plotWidth / candles.length;
    const xForIndex = (index) => geometry.left + step * (index + 0.5);

    drawGrid(geometry, minPrice, maxPrice, candles, xForIndex);
    drawVolumes(geometry, candles, xForIndex, step);
    drawCandles(geometry, candles, xForIndex, yForPrice, step);
    drawMaLine(context, candles, "ma99", COLORS.ma99, xForIndex, yForPrice);
    drawMaLine(context, candles, "ma20", COLORS.ma20, xForIndex, yForPrice);
    drawMaLine(context, candles, "ma7", COLORS.ma7, xForIndex, yForPrice);

    if (state.hoverIndex !== null && candles[state.hoverIndex]) {
        drawCrosshair(geometry, candles, state.hoverIndex, xForIndex, yForPrice);
    }

    elements.chartCanvas._chartMeta = {
        candles,
        step,
        xForIndex,
        yForPrice,
        geometry,
    };
}

function drawGrid(geometry, minPrice, maxPrice, candles, xForIndex) {
    const { context } = geometry;
    context.save();
    context.lineWidth = 1;
    context.font = "10px ui-sans-serif, system-ui";
    context.textBaseline = "middle";

    for (let index = 0; index <= 5; index += 1) {
        const ratio = index / 5;
        const y = geometry.top + ratio * (geometry.priceBottom - geometry.top);
        const price = maxPrice - ratio * (maxPrice - minPrice);
        context.strokeStyle = COLORS.grid;
        context.beginPath();
        context.moveTo(geometry.left, Math.round(y) + 0.5);
        context.lineTo(geometry.width - geometry.right, Math.round(y) + 0.5);
        context.stroke();
        context.fillStyle = COLORS.axis;
        context.fillText(formatPrice(price), geometry.width - geometry.right + 8, y);
    }

    const tickCount = 6;
    for (let index = 0; index < tickCount; index += 1) {
        const candleIndex = Math.round((index / (tickCount - 1)) * (candles.length - 1));
        const x = xForIndex(candleIndex);
        context.strokeStyle = COLORS.grid;
        context.beginPath();
        context.moveTo(Math.round(x) + 0.5, geometry.top);
        context.lineTo(Math.round(x) + 0.5, geometry.volumeBottom);
        context.stroke();
        context.fillStyle = COLORS.axis;
        context.textAlign = index === 0 ? "left" : index === tickCount - 1 ? "right" : "center";
        context.fillText(
            formatChartDate(candles[candleIndex].time),
            x,
            geometry.height - geometry.bottom / 2,
        );
    }
    context.restore();
}

function drawVolumes(geometry, candles, xForIndex, step) {
    const maxVolume = Math.max(...candles.map((candle) => candle.volume), 1);
    const width = Math.max(1, Math.min(7, step * 0.64));
    const { context } = geometry;

    candles.forEach((candle, index) => {
        const height = (candle.volume / maxVolume) * (geometry.volumeHeight - 8);
        context.fillStyle = candle.close >= candle.open
            ? "rgb(10 155 97 / 24%)"
            : "rgb(220 63 86 / 22%)";
        context.fillRect(
            xForIndex(index) - width / 2,
            geometry.volumeBottom - height,
            width,
            height,
        );
    });
}

function drawCandles(geometry, candles, xForIndex, yForPrice, step) {
    const { context } = geometry;
    const bodyWidth = Math.max(1, Math.min(7, step * 0.68));

    candles.forEach((candle, index) => {
        const x = xForIndex(index);
        const openY = yForPrice(candle.open);
        const closeY = yForPrice(candle.close);
        const highY = yForPrice(candle.high);
        const lowY = yForPrice(candle.low);
        const color = candle.close >= candle.open ? COLORS.up : COLORS.down;

        context.strokeStyle = color;
        context.fillStyle = color;
        context.lineWidth = 1;
        context.beginPath();
        context.moveTo(Math.round(x) + 0.5, highY);
        context.lineTo(Math.round(x) + 0.5, lowY);
        context.stroke();

        const bodyTop = Math.min(openY, closeY);
        const bodyHeight = Math.max(1, Math.abs(openY - closeY));
        context.fillRect(x - bodyWidth / 2, bodyTop, bodyWidth, bodyHeight);
    });
}

function drawMaLine(context, candles, key, color, xForIndex, yForPrice) {
    context.save();
    context.strokeStyle = color;
    context.lineWidth = 1.35;
    context.lineJoin = "round";
    context.lineCap = "round";
    context.beginPath();

    let started = false;
    candles.forEach((candle, index) => {
        const value = candle[key];
        if (value === null || !Number.isFinite(value)) {
            started = false;
            return;
        }
        const x = xForIndex(index);
        const y = yForPrice(value);
        if (!started) {
            context.moveTo(x, y);
            started = true;
        } else {
            context.lineTo(x, y);
        }
    });
    context.stroke();
    context.restore();
}

function drawCrosshair(geometry, candles, index, xForIndex, yForPrice) {
    const candle = candles[index];
    const x = xForIndex(index);
    const y = yForPrice(candle.close);
    const { context } = geometry;

    context.save();
    context.setLineDash([4, 4]);
    context.strokeStyle = COLORS.crosshair;
    context.lineWidth = 1;
    context.beginPath();
    context.moveTo(x, geometry.top);
    context.lineTo(x, geometry.volumeBottom);
    context.moveTo(geometry.left, y);
    context.lineTo(geometry.width - geometry.right, y);
    context.stroke();
    context.restore();
}

function formatChartDate(timestamp) {
    const date = new Date(timestamp * 1000);
    const options = state.timeframe === "1d"
        ? { month: "2-digit", day: "2-digit" }
        : { day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false };
    return new Intl.DateTimeFormat("zh-CN", options).format(date);
}

function showTooltip(event) {
    const meta = elements.chartCanvas._chartMeta;
    if (!meta?.candles.length) return;

    const rect = elements.chartCanvas.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const rawIndex = Math.floor((x - meta.geometry.left) / meta.step);
    const index = Math.max(0, Math.min(meta.candles.length - 1, rawIndex));
    state.hoverIndex = index;
    drawChart();

    const candle = meta.candles[index];
    const change = candle.changeToday;
    const changeText = change === null
        ? "--"
        : `${change >= 0 ? "+" : ""}${change.toFixed(2)}%`;
    const changeClass = change !== null && change >= 0 ? "positive" : "negative";
    elements.chartTooltip.innerHTML = [
        `<strong>${formatTooltipDate(candle.time)}</strong>`,
        `开 ${formatPrice(candle.open)}　高 ${formatPrice(candle.high)}`,
        `低 ${formatPrice(candle.low)}　收 ${formatPrice(candle.close)}`,
        `今日涨跌 <span class="${changeClass}">${changeText}</span>`,
        `MA7 ${formatPrice(candle.ma7)}　MA20 ${formatPrice(candle.ma20)}`,
        `MA99 ${formatPrice(candle.ma99)}`,
        `成交量 ${formatCompact(candle.volume)}`,
        `成交额 ${formatQuoteVolume(candle.quoteVolume)} USDT`,
    ].join("<br>");

    const tooltipWidth = 190;
    const tooltipHeight = 148;
    elements.chartTooltip.style.left = `${Math.min(rect.width - tooltipWidth - 8, Math.max(8, x + 14))}px`;
    elements.chartTooltip.style.top = `${Math.min(rect.height - tooltipHeight - 8, Math.max(8, event.clientY - rect.top + 12))}px`;
    elements.chartTooltip.style.display = "block";
}

function formatTooltipDate(timestamp) {
    return new Intl.DateTimeFormat("zh-CN", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: state.timeframe === "1d" ? undefined : "2-digit",
        minute: state.timeframe === "1d" ? undefined : "2-digit",
        hour12: false,
    }).format(new Date(timestamp * 1000));
}

function zoomChart(event) {
    if (!state.candles.length) return;
    event.preventDefault();

    const meta = elements.chartCanvas._chartMeta;
    if (!meta) return;

    const isHorizontalPan = !event.ctrlKey && Math.abs(event.deltaX) > Math.abs(event.deltaY);
    if (isHorizontalPan) {
        panChartByPixels(event.deltaX, meta);
        return;
    }

    const rect = elements.chartCanvas.getBoundingClientRect();
    const pointerRatio = Math.max(
        0,
        Math.min(
            1,
            (event.clientX - rect.left - meta.geometry.left) / meta.geometry.plotWidth,
        ),
    );
    const isZoomingIn = event.deltaY < 0;
    zoomToCount(
        Math.round(
            Math.min(
                state.viewCount || state.candles.length,
                state.candles.length,
            ) * (isZoomingIn ? 0.82 : 1.22),
        ),
        pointerRatio,
        isZoomingIn,
    );
}

function panChartByPixels(deltaX, meta) {
    state.trackpadPanRemainder += deltaX / meta.step;
    const candleShift = Math.trunc(state.trackpadPanRemainder);
    if (candleShift === 0) return;

    state.trackpadPanRemainder -= candleShift;
    const count = Math.min(state.viewCount || state.candles.length, state.candles.length);
    const currentEnd = Math.min(
        state.viewEnd || state.candles.length,
        state.candles.length,
    );
    state.viewEnd = Math.max(
        count,
        Math.min(state.candles.length, currentEnd + candleShift),
    );
    state.hoverIndex = null;
    elements.chartTooltip.style.display = "none";
    drawChart();
}

function zoomToCount(requestedCount, pointerRatio = 0.5, alignLatest = false) {
    const currentCount = Math.min(
        state.viewCount || state.candles.length,
        state.candles.length,
    );
    const currentEnd = Math.min(
        state.viewEnd || state.candles.length,
        state.candles.length,
    );
    const currentStart = Math.max(0, currentEnd - currentCount);
    const nextCount = Math.max(
        30,
        Math.min(state.candles.length, requestedCount),
    );
    if (alignLatest) {
        state.viewCount = nextCount;
        state.viewEnd = state.candles.length;
        state.hoverIndex = null;
        elements.chartTooltip.style.display = "none";
        drawChart();
        return;
    }

    const anchorIndex = currentStart + pointerRatio * currentCount;
    let nextStart = Math.round(anchorIndex - pointerRatio * nextCount);
    nextStart = Math.max(0, Math.min(state.candles.length - nextCount, nextStart));

    state.viewCount = nextCount;
    state.viewEnd = nextStart + nextCount;
    state.hoverIndex = null;
    elements.chartTooltip.style.display = "none";
    drawChart();
}

function startChartDrag(event) {
    if (event.button !== 0 || !state.candles.length) return;
    state.dragging = true;
    state.dragStartX = event.clientX;
    state.dragStartEnd = state.viewEnd || state.candles.length;
    elements.chartCard.classList.add("dragging");
}

function dragChart(event) {
    if (!state.dragging) {
        showTooltip(event);
        return;
    }

    const meta = elements.chartCanvas._chartMeta;
    if (!meta) return;
    const movedCandles = Math.round((event.clientX - state.dragStartX) / meta.step);
    const count = Math.min(state.viewCount || state.candles.length, state.candles.length);
    state.viewEnd = Math.max(
        count,
        Math.min(state.candles.length, state.dragStartEnd - movedCandles),
    );
    state.hoverIndex = null;
    elements.chartTooltip.style.display = "none";
    drawChart();
}

function stopChartDrag() {
    if (!state.dragging) return;
    state.dragging = false;
    elements.chartCard.classList.remove("dragging");
}

elements.settingsTabs.addEventListener("click", (event) => {
    const tab = event.target.closest("[data-settings-tab]");
    if (!tab) return;
    activateSettingsTab(tab.dataset.settingsTab);
});
elements.settingsTabs.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
        return;
    }
    event.preventDefault();
    const tabs = Array.from(
        elements.settingsTabs.querySelectorAll("[data-settings-tab]"),
    );
    const currentIndex = tabs.findIndex(
        (tab) => tab.dataset.settingsTab === state.settingsTab,
    );
    let nextIndex = currentIndex;
    if (event.key === "ArrowLeft") {
        nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
    } else if (event.key === "ArrowRight") {
        nextIndex = (currentIndex + 1) % tabs.length;
    } else if (event.key === "Home") {
        nextIndex = 0;
    } else if (event.key === "End") {
        nextIndex = tabs.length - 1;
    }
    activateSettingsTab(tabs[nextIndex].dataset.settingsTab, true);
});
elements.searchInput.addEventListener("input", filterCandidates);
elements.sortSelect.addEventListener("change", () => {
    state.sortKey = elements.sortSelect.value;
    filterCandidates();
});
elements.entryEnabledInput.addEventListener("change", () => {
    updateFilterRuleValues();
    markSettingsDirty();
});
elements.lookbackDaysInput.addEventListener("input", () => {
    updateFilterRuleValues();
    markSettingsDirty();
});
elements.lookbackDaysInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
        elements.lookbackDaysInput.blur();
    }
});
elements.minChange20dInput.addEventListener("input", () => {
    updateFilterRuleValues();
    markSettingsDirty();
});
elements.minChange20dInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
        elements.minChange20dInput.blur();
    }
});
elements.maxChange20dInput.addEventListener("input", () => {
    updateFilterRuleValues();
    markSettingsDirty();
});
elements.maxChange20dInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
        elements.maxChange20dInput.blur();
    }
});
elements.maxDrawdownToGainRatioInput.addEventListener("input", () => {
    updateFilterRuleValues();
    markSettingsDirty();
});
elements.maxDrawdownToGainRatioInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
        elements.maxDrawdownToGainRatioInput.blur();
    }
});
elements.useMa99FilterInput.addEventListener("change", () => {
    updateFilterRuleValues();
    markSettingsDirty();
});
elements.ma7ReclaimEnabledInput.addEventListener("change", () => {
    updateFilterRuleValues();
    markSettingsDirty();
});
elements.ma7ReclaimToleranceInput.addEventListener("input", () => {
    updateFilterRuleValues();
    markSettingsDirty();
});
elements.ma7ReclaimLookbackInput.addEventListener("input", () => {
    updateFilterRuleValues();
    markSettingsDirty();
});
elements.ma7ExitThresholdInput.addEventListener("input", () => {
    updateFilterRuleValues();
    markSettingsDirty();
});
elements.ma7ExitThresholdInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
        elements.ma7ExitThresholdInput.blur();
    }
});
elements.hardStoplossInput.addEventListener("input", () => {
    updateFilterRuleValues();
    markSettingsDirty();
});
elements.noProgressExitEnabledInput.addEventListener("change", () => {
    updateFilterRuleValues();
    markSettingsDirty();
});
elements.peakDrawdownStopInput.addEventListener("input", () => {
    updateFilterRuleValues();
    markSettingsDirty();
});
elements.peakDrawdownStopInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
        elements.peakDrawdownStopInput.blur();
    }
});
elements.drawdownStopModeInput.addEventListener("change", () => {
    updateFilterRuleValues();
    markSettingsDirty();
});
elements.dynamicDrawdownActivationInput.addEventListener("input", () => {
    updateFilterRuleValues();
    markSettingsDirty();
});
elements.dynamicDrawdownActivationInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
        elements.dynamicDrawdownActivationInput.blur();
    }
});
elements.dynamicMaxProfitGivebackInput.addEventListener("input", () => {
    updateFilterRuleValues();
    markSettingsDirty();
});
elements.dynamicMaxProfitGivebackInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
        elements.dynamicMaxProfitGivebackInput.blur();
    }
});
elements.chandelierExitEnabledInput.addEventListener("change", () => {
    updateFilterRuleValues();
    markSettingsDirty();
});
elements.partialTakeProfitEnabledInput.addEventListener("change", () => {
    updateFilterRuleValues();
    markSettingsDirty();
});
elements.cooldownEnabledInput.addEventListener("change", () => {
    updateFilterRuleValues();
    markSettingsDirty();
});
elements.candidateReentryRequiredInput.addEventListener("change", () => {
    updateFilterRuleValues();
    markSettingsDirty();
});
elements.cooldownHoursInput.addEventListener("input", () => {
    updateFilterRuleValues();
    markSettingsDirty();
});
elements.cooldownHoursInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
        elements.cooldownHoursInput.blur();
    }
});
elements.hardStoplossInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
        elements.hardStoplossInput.blur();
    }
});
elements.scanIntervalInput.addEventListener("input", markSettingsDirty);
elements.scanIntervalInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
        elements.scanIntervalInput.blur();
    }
});
elements.strategyTimeframeInput.addEventListener("change", (event) => {
    applyStrategyPreset(event.target.value);
});
elements.saveSettingsButton.addEventListener("click", saveSettings);
elements.refreshButton.addEventListener("click", startManualScan);
elements.refreshChartButton.addEventListener("click", async () => {
    if (elements.refreshChartButton.disabled) return;
    elements.refreshChartButton.disabled = true;
    elements.refreshChartButton.textContent = "刷新中";
    try {
        const refreshed = await loadChart({ forceRefresh: true });
        elements.refreshChartButton.textContent = refreshed ? "已刷新" : "刷新失败";
        window.setTimeout(() => {
            elements.refreshChartButton.textContent = "刷新 K 线";
        }, 1200);
    } finally {
        elements.refreshChartButton.disabled = false;
    }
});
elements.zoomInButton.addEventListener("click", () => {
    zoomToCount(
        Math.round((state.viewCount || state.candles.length) * 0.72),
        1,
        true,
    );
});
elements.zoomOutButton.addEventListener("click", () => {
    zoomToCount(Math.round((state.viewCount || state.candles.length) * 1.38));
});
elements.resetChartButton.addEventListener("click", () => {
    resetChartView();
    drawChart();
});
elements.timeframeTabs.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-timeframe]");
    if (!button || button.dataset.timeframe === state.timeframe) return;
    state.timeframe = button.dataset.timeframe;
    elements.timeframeTabs.querySelectorAll("button").forEach((item) => {
        item.classList.toggle("active", item === button);
    });
    await loadChart({ resetView: true });
});

elements.chartCanvas.addEventListener("wheel", zoomChart, { passive: false });
elements.chartCanvas.addEventListener("mousedown", startChartDrag);
elements.chartCanvas.addEventListener("mousemove", dragChart);
window.addEventListener("mouseup", stopChartDrag);
elements.chartCanvas.addEventListener("mouseleave", () => {
    stopChartDrag();
    state.hoverIndex = null;
    elements.chartTooltip.style.display = "none";
    drawChart();
});
elements.backtestEquityCanvas.addEventListener("mousemove", (event) => {
    updateBacktestEquityHover(event.clientX);
});
elements.backtestEquityCanvas.addEventListener(
    "touchstart",
    (event) => {
        if (!event.touches.length) return;
        event.preventDefault();
        updateBacktestEquityHover(event.touches[0].clientX);
    },
    { passive: false },
);
elements.backtestEquityCanvas.addEventListener(
    "touchmove",
    (event) => {
        if (!event.touches.length) return;
        event.preventDefault();
        updateBacktestEquityHover(event.touches[0].clientX);
    },
    { passive: false },
);
elements.backtestEquityCanvas.addEventListener(
    "mouseleave",
    clearBacktestEquityHover,
);
elements.backtestTableWrap.addEventListener(
    "scroll",
    loadMoreBacktestTrades,
);

const resizeObserver = new ResizeObserver(() => drawChart());
resizeObserver.observe(elements.chartCanvas.parentElement);
const backtestResizeObserver = new ResizeObserver(() => drawBacktestEquity());
backtestResizeObserver.observe(elements.backtestEquityCanvas.parentElement);

elements.viewTabs.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-view]");
    if (!button) return;
    const showBacktest = button.dataset.view === "backtest";
    elements.liveWorkspace.classList.toggle("is-hidden", showBacktest);
    elements.backtestWorkspace.classList.toggle("is-hidden", !showBacktest);
    elements.viewTabs.querySelectorAll("button").forEach((item) => {
        item.classList.toggle("active", item === button);
    });
    if (showBacktest) {
        loadBacktestStatus();
        requestAnimationFrame(drawBacktestEquity);
    }
});
elements.runBacktestButton.addEventListener("click", startBacktest);
elements.cancelBacktestButton.addEventListener("click", cancelBacktest);

elements.loginForm.addEventListener("submit", submitLogin);
elements.logoutButton.addEventListener("click", () => {
    showLogin("已退出登录");
});

function initializeApp() {
    if (appInitialized) return;
    appInitialized = true;
    initializeSettingsTabs();
    loadSettings();
    loadCandidates({ preserveSelection: false, reloadChart: true });
    loadBacktestStatus();
    setInterval(() => loadCandidates(), 60_000);
}

bootstrapAuth();

async function startManualScan() {
    if (elements.refreshButton.disabled) return;

    elements.refreshButton.disabled = true;
    elements.saveSettingsButton.disabled = true;
    elements.refreshButton.classList.add("loading");
    elements.refreshButtonLabel.textContent = "扫描中";
    setConnectionStatus("", state.settingsDirty ? "正在预览草稿参数" : "正在扫描");

    try {
        const request = {
            method: "POST",
            headers: { "Content-Type": "application/json" },
        };
        if (state.settingsDirty) {
            request.body = JSON.stringify(currentSettingsPayload());
        }
        let status = await fetchJson("/api/scan", request);
        while (status.running) {
            await new Promise((resolve) => window.setTimeout(resolve, 1000));
            status = await fetchJson("/api/scan/status");
        }

        if (status.return_code === 0) {
            elements.settingsStatus.textContent = status.is_preview
                ? "预览完成；满意后点击“保存参数”用于交易"
                : "正式候选已更新";
            state.candleCache.clear();
            await loadCandidates({ reloadChart: true });
        } else {
            setConnectionStatus("error", status.message || "扫描失败");
        }
    } catch (error) {
        setConnectionStatus("error", `扫描失败：${error.message}`);
    } finally {
        elements.refreshButton.disabled = false;
        elements.saveSettingsButton.disabled = !state.settingsDirty;
        elements.refreshButton.classList.remove("loading");
        elements.refreshButtonLabel.textContent = "立即扫描";
    }
}
