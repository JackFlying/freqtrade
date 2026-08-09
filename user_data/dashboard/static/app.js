const COLORS = {
    grid: "#1b2b36",
    axis: "#718692",
    up: "#39d98a",
    down: "#ff5c72",
    ma7: "#4cc9f0",
    ma20: "#a78bfa",
    ma99: "#f973a7",
    crosshair: "#708895",
};

const state = {
    candidates: [],
    filteredCandidates: [],
    selectedPair: null,
    timeframe: "1d",
    candles: [],
    chartRequest: 0,
    hoverIndex: null,
    viewCount: null,
    viewEnd: null,
    dragging: false,
    dragStartX: 0,
    dragStartEnd: 0,
    trackpadPanRemainder: 0,
};

const elements = {
    statusDot: document.querySelector("#statusDot"),
    statusText: document.querySelector("#statusText"),
    updatedAt: document.querySelector("#updatedAt"),
    candidateCount: document.querySelector("#candidateCount"),
    candidateList: document.querySelector("#candidateList"),
    refreshButton: document.querySelector("#refreshButton"),
    refreshButtonLabel: document.querySelector("#refreshButton span"),
    minChange20dInput: document.querySelector("#minChange20dInput"),
    maxDrawdownInput: document.querySelector("#maxDrawdownInput"),
    settingsStatus: document.querySelector("#settingsStatus"),
    minChangeRule: document.querySelector("#minChangeRule"),
    maxDrawdownRule: document.querySelector("#maxDrawdownRule"),
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
    volume24h: document.querySelector("#volume24h"),
    chartCanvas: document.querySelector("#chartCanvas"),
    chartLoading: document.querySelector("#chartLoading"),
    chartTooltip: document.querySelector("#chartTooltip"),
    chartCard: document.querySelector(".chart-card"),
    refreshChartButton: document.querySelector("#refreshChartButton"),
    resetChartButton: document.querySelector("#resetChartButton"),
    zoomOutButton: document.querySelector("#zoomOutButton"),
    zoomInButton: document.querySelector("#zoomInButton"),
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
    const response = await fetch(url, { cache: "no-store", ...options });
    if (!response.ok) {
        let detail = `${response.status} ${response.statusText}`;
        try {
            const payload = await response.json();
            detail = payload.detail || detail;
        } catch {
            // Keep the HTTP status when the response is not JSON.
        }
        throw new Error(detail);
    }
    return response.json();
}

async function loadCandidates({
    preserveSelection = true,
    reloadChart = false,
} = {}) {
    try {
        const payload = await fetchJson("/api/candidates");
        state.candidates = payload.candidates || [];
        elements.candidateCount.textContent = String(state.candidates.length);
        elements.updatedAt.textContent = formatUpdatedAt(payload.status?.updated_at);
        elements.updatedAt.title = payload.status?.updated_at || "";
        setConnectionStatus("online", "数据正常");

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
        elements.minChange20dInput.value = settings.min_change_20d;
        elements.maxDrawdownInput.value = settings.max_drawdown_20d;
        updateFilterRuleValues();
    } catch (error) {
        elements.settingsStatus.textContent = `读取失败：${error.message}`;
    }
}

function updateFilterRuleValues() {
    const minChange = Number(elements.minChange20dInput.value);
    const maxDrawdown = Number(elements.maxDrawdownInput.value);
    elements.minChangeRule.textContent = Number.isFinite(minChange)
        ? `${minChange}%`
        : "--";
    elements.maxDrawdownRule.textContent = Number.isFinite(maxDrawdown)
        ? `${maxDrawdown}%`
        : "--";
}

async function saveSettings() {
    const minChange20d = Number(elements.minChange20dInput.value);
    const maxDrawdown20d = Number(elements.maxDrawdownInput.value);
    if (!Number.isFinite(minChange20d) || minChange20d < -100 || minChange20d > 10000) {
        elements.settingsStatus.textContent = "涨幅请输入 -100～10000";
        return;
    }
    if (!Number.isFinite(maxDrawdown20d) || maxDrawdown20d < 0 || maxDrawdown20d > 100) {
        elements.settingsStatus.textContent = "回撤请输入 0～100";
        return;
    }

    elements.settingsStatus.textContent = "保存中...";
    try {
        await fetchJson("/api/settings", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                min_change_20d: minChange20d,
                max_drawdown_20d: maxDrawdown20d,
            }),
        });
        updateFilterRuleValues();
        elements.settingsStatus.textContent = "已保存，扫描后生效";
    } catch (error) {
        elements.settingsStatus.textContent = `保存失败：${error.message}`;
    }
}

function filterCandidates() {
    const keyword = elements.searchInput.value.trim().toUpperCase();
    state.filteredCandidates = state.candidates.filter((candidate) =>
        candidate.pair.toUpperCase().includes(keyword),
    );
    renderCandidateList();
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
        volume.textContent = `24h ${formatQuoteVolume(candidate.quote_volume_24h)} USDT`;
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

    const requestId = ++state.chartRequest;
    elements.chartLoading.textContent = "正在加载 K 线...";
    elements.chartLoading.classList.remove("hidden");
    elements.chartTooltip.style.display = "none";

    try {
        const params = new URLSearchParams({
            pair,
            timeframe: state.timeframe,
            limit: "365",
        });
        if (forceRefresh) params.set("refresh", "true");
        const payload = await fetchJson(`/api/candles?${params}`);
        if (requestId !== state.chartRequest) return;
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
        elements.chartLoading.classList.add("hidden");
        return true;
    } catch (error) {
        if (requestId !== state.chartRequest) return;
        elements.chartLoading.textContent = `K 线加载失败：${error.message}`;
        elements.chartLoading.classList.remove("hidden");
        return false;
    }
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
            ? "rgb(57 217 138 / 28%)"
            : "rgb(255 92 114 / 25%)";
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

elements.searchInput.addEventListener("input", filterCandidates);
elements.minChange20dInput.addEventListener("change", saveSettings);
elements.minChange20dInput.addEventListener("input", updateFilterRuleValues);
elements.minChange20dInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
        elements.minChange20dInput.blur();
    }
});
elements.maxDrawdownInput.addEventListener("change", saveSettings);
elements.maxDrawdownInput.addEventListener("input", updateFilterRuleValues);
elements.maxDrawdownInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
        elements.maxDrawdownInput.blur();
    }
});
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

const resizeObserver = new ResizeObserver(() => drawChart());
resizeObserver.observe(elements.chartCanvas.parentElement);

loadSettings();
loadCandidates({ preserveSelection: false, reloadChart: true });
setInterval(() => loadCandidates(), 60_000);

async function startManualScan() {
    if (elements.refreshButton.disabled) return;

    elements.refreshButton.disabled = true;
    elements.refreshButton.classList.add("loading");
    elements.refreshButtonLabel.textContent = "扫描中";
    setConnectionStatus("", "正在扫描");

    try {
        let status = await fetchJson("/api/scan", { method: "POST" });
        while (status.running) {
            await new Promise((resolve) => window.setTimeout(resolve, 1000));
            status = await fetchJson("/api/scan/status");
        }

        if (status.return_code === 0) {
            setConnectionStatus("online", "扫描完成");
            elements.settingsStatus.textContent = "已按当前 X 筛选";
            await loadCandidates();
        } else {
            setConnectionStatus("error", status.message || "扫描失败");
        }
    } catch (error) {
        setConnectionStatus("error", `扫描失败：${error.message}`);
    } finally {
        elements.refreshButton.disabled = false;
        elements.refreshButton.classList.remove("loading");
        elements.refreshButtonLabel.textContent = "立即扫描";
    }
}
