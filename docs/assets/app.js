(() => {
  "use strict";

  const DATA_URL = "./data/promotions.json";
  const UPCOMING_DAYS = 14;
  const ENDING_DAYS = 7;
  const DAY_MS = 24 * 60 * 60 * 1000;
  const THEME_KEY = "twpay-theme";
  const THEME_ORDER = ["system", "light", "dark"];

  const themeLabels = {
    system: { short: "系統", icon: "◐", description: "跟隨系統" },
    light: { short: "淺色", icon: "☀", description: "淺色" },
    dark: { short: "深色", icon: "☾", description: "深色" }
  };

  const categoryLabels = {
    featured: "今日精選",
    "high-return": "高回饋活動",
    upcoming: "即將開始",
    ending: "即將結束",
    "sold-out": "額滿提醒",
    all: "所有活動"
  };

  const lifecycleLabels = {
    active: { label: "進行中", className: "is-active" },
    upcoming: { label: "即將開始", className: "is-upcoming" },
    ended: { label: "已結束", className: "" },
    unknown: { label: "期間待確認", className: "" }
  };

  const quotaLabels = {
    sold_out: { label: "已額滿", className: "is-sold" },
    partial_sold_out: { label: "部分額滿", className: "is-partial" },
    unknown_app_only: { label: "請至 App 確認", className: "is-app" },
    unknown_source_failure: { label: "額滿來源暫時無法讀取", className: "is-app" },
    not_marked_full: { label: "官網未標示額滿", className: "is-open" },
    confirmed_available: { label: "尚有名額", className: "is-open" },
    unknown: { label: "名額待確認", className: "" }
  };

  const insightKeyLabels = {
    high_return: "高回饋",
    is_high_return: "高回饋",
    upcoming: "即將開始",
    ending_soon: "即將結束",
    featured: "重點活動",
    highlight: "重點活動",
    return_rate: "回饋比例",
    reward: "回饋內容",
    reward_cap: "回饋上限",
    cap: "回饋上限",
    eligibility: "適用對象",
    channel: "適用通路",
    payment_method: "付款方式",
    note: "提醒"
  };

  const state = {
    activities: [],
    highlights: null,
    providerCoverage: [],
    category: "featured",
    query: "",
    provider: ""
  };

  const elements = {
    sourceHealth: document.querySelector("#source-health"),
    sourceHealthText: document.querySelector("#source-health-text"),
    updatedAt: document.querySelector("#updated-at"),
    dailyHeadline: document.querySelector("#daily-headline"),
    themeToggle: document.querySelector("#theme-toggle"),
    themeIcon: document.querySelector("#theme-icon"),
    themeLabel: document.querySelector("#theme-label"),
    themeColor: document.querySelector("#theme-color"),
    summaryList: document.querySelector("#summary-list"),
    searchInput: document.querySelector("#search-input"),
    providerSelect: document.querySelector("#provider-select"),
    categoryTabs: document.querySelector("#category-tabs"),
    clearFilters: document.querySelector("#clear-filters"),
    resultsContext: document.querySelector("#results-context"),
    resultsCount: document.querySelector("#results-count"),
    activityList: document.querySelector("#activity-list"),
    emptyState: document.querySelector("#empty-state"),
    emptyClear: document.querySelector("#empty-clear"),
    errorState: document.querySelector("#error-state"),
    errorMessage: document.querySelector("#error-message"),
    retryButton: document.querySelector("#retry-button"),
    coverageSection: document.querySelector("#coverage-section"),
    coverageSummary: document.querySelector("#coverage-summary"),
    providerCoverageList: document.querySelector("#provider-coverage-list"),
    cardTemplate: document.querySelector("#activity-card-template")
  };

  const dateFormatter = new Intl.DateTimeFormat("zh-TW", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    timeZone: "Asia/Taipei"
  });

  const dateTimeFormatter = new Intl.DateTimeFormat("zh-TW", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Taipei"
  });

  const collator = new Intl.Collator("zh-Hant", { numeric: true, sensitivity: "base" });
  const systemThemeQuery = window.matchMedia("(prefers-color-scheme: dark)");

  function currentThemePreference() {
    const preference = document.documentElement.dataset.themePreference;
    return THEME_ORDER.includes(preference) ? preference : "system";
  }

  function resolvedTheme(preference) {
    return preference === "system" ? (systemThemeQuery.matches ? "dark" : "light") : preference;
  }

  function applyTheme(preference, persist = false) {
    const safePreference = THEME_ORDER.includes(preference) ? preference : "system";
    const resolved = resolvedTheme(safePreference);
    const nextPreference = THEME_ORDER[(THEME_ORDER.indexOf(safePreference) + 1) % THEME_ORDER.length];
    document.documentElement.dataset.theme = resolved;
    document.documentElement.dataset.themePreference = safePreference;
    document.documentElement.style.colorScheme = resolved;
    elements.themeColor.content = resolved === "dark" ? "#0b1b25" : "#173f5f";
    elements.themeIcon.textContent = themeLabels[safePreference].icon;
    elements.themeLabel.textContent = themeLabels[safePreference].short;
    elements.themeToggle.setAttribute(
      "aria-label",
      `主題：${themeLabels[safePreference].description}；點擊切換為${themeLabels[nextPreference].description}`
    );
    elements.themeToggle.title = elements.themeToggle.getAttribute("aria-label");
    if (persist) {
      try {
        localStorage.setItem(THEME_KEY, safePreference);
      } catch {}
    }
  }

  function cycleTheme() {
    const preference = currentThemePreference();
    const nextPreference = THEME_ORDER[(THEME_ORDER.indexOf(preference) + 1) % THEME_ORDER.length];
    applyTheme(nextPreference, true);
  }

  function normalizeKey(value) {
    return String(value ?? "").trim().toLowerCase();
  }

  function parseDate(value) {
    if (!value) return null;
    const text = String(value).trim();
    const simpleDate = /^\d{4}-\d{2}-\d{2}$/.test(text);
    const date = new Date(simpleDate ? `${text}T00:00:00+08:00` : text);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function startOfToday() {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: "Asia/Taipei",
      year: "numeric",
      month: "2-digit",
      day: "2-digit"
    }).formatToParts(new Date()).reduce((result, part) => {
      if (part.type !== "literal") result[part.type] = part.value;
      return result;
    }, {});
    return new Date(`${parts.year}-${parts.month}-${parts.day}T00:00:00+08:00`);
  }

  function formatDate(value) {
    const date = parseDate(value);
    return date ? dateFormatter.format(date) : "日期待確認";
  }

  function formatPeriod(activity) {
    const start = activity.start_date ? formatDate(activity.start_date) : "開始日待確認";
    const end = activity.end_date ? formatDate(activity.end_date) : "結束日待確認";
    if (activity.start_date && activity.end_date && activity.start_date === activity.end_date) {
      return start;
    }
    return `${start}－${end}`;
  }

  function stringValue(value) {
    if (value === null || value === undefined) return "";
    if (typeof value === "string" || typeof value === "number") return String(value).trim();
    return "";
  }

  function uniqueStrings(values) {
    const seen = new Set();
    return values.filter((value) => {
      const key = normalizeKey(value);
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  function humanizeKey(key) {
    if (insightKeyLabels[key]) return insightKeyLabels[key];
    return key.replaceAll("_", " ").trim();
  }

  function normalizeTextList(value, parentKey = "") {
    if (value === null || value === undefined || value === false) return [];
    const primitive = stringValue(value);
    if (primitive) {
      if (value === true) return parentKey ? [humanizeKey(parentKey)] : [];
      return [primitive];
    }
    if (value === true) return parentKey ? [humanizeKey(parentKey)] : [];
    if (Array.isArray(value)) {
      return uniqueStrings(value.flatMap((item) => normalizeTextList(item, parentKey)));
    }
    if (typeof value === "object") {
      const preferred = ["text", "label", "title", "summary", "value"];
      const direct = preferred.map((key) => stringValue(value[key])).filter(Boolean);
      if (direct.length) return uniqueStrings(direct);
      return uniqueStrings(Object.entries(value).flatMap(([key, item]) => {
        if (["id", "url", "type", "category"].includes(key)) return [];
        const items = normalizeTextList(item, key);
        if (items.length === 1 && typeof item !== "boolean" && typeof item !== "object") {
          return [`${humanizeKey(key)}：${items[0]}`];
        }
        return items;
      }));
    }
    return [];
  }

  function normalizeInsights(activity) {
    const insights = activity.insights;
    if (!insights || typeof insights !== "object" || Array.isArray(insights)) {
      return normalizeTextList(insights);
    }

    const values = [];
    const editorialSummary = stringValue(activity.editorial_summary);
    if (editorialSummary) values.push(editorialSummary);
    const summary = stringValue(insights.human_summary);
    if (summary) values.push(summary);
    if (!summary && insights.max_reward_percent != null && Number.isFinite(Number(insights.max_reward_percent))) {
      const percent = Number(insights.max_reward_percent).toLocaleString("zh-TW", { maximumFractionDigits: 2 });
      values.push(`最高 ${percent}% 回饋`);
    }
    if (!summary && insights.fixed_reward_amount != null && Number.isFinite(Number(insights.fixed_reward_amount))) {
      const amount = Number(insights.fixed_reward_amount).toLocaleString("zh-TW", { maximumFractionDigits: 2 });
      values.push(`固定回饋最高 ${amount} 元`);
    }
    return uniqueStrings(values.length ? values : normalizeTextList(insights));
  }

  function normalizeInsightTags(activity) {
    const tags = activity.insights && typeof activity.insights === "object"
      ? normalizeTextList(activity.insights.insight_tags)
      : [];
    return tags.length ? tags : normalizeInsights(activity).slice(0, 2);
  }

  function normalizeConditions(activity) {
    const display = normalizeTextList(activity.conditions_display);
    return display.length ? display : normalizeTextList(activity.conditions_summary);
  }

  function activitySearchText(activity) {
    return [
      activity.provider_name,
      activity.title,
      ...normalizeConditions(activity),
      ...normalizeInsights(activity)
    ].filter(Boolean).join(" ").toLocaleLowerCase("zh-Hant");
  }

  function nestedBoolean(value, keys) {
    if (!value || typeof value !== "object") return false;
    return keys.some((key) => value[key] === true) || Object.values(value).some((item) => {
      if (!item || typeof item !== "object") return false;
      return nestedBoolean(item, keys);
    });
  }

  function collectHighlightReferences(value, set) {
    if (value === null || value === undefined) return;
    if (typeof value === "string" || typeof value === "number") {
      const reference = normalizeKey(value);
      if (reference) set.add(reference);
      return;
    }
    if (Array.isArray(value)) {
      value.forEach((item) => collectHighlightReferences(item, set));
      return;
    }
    if (typeof value === "object") {
      ["id", "external_id", "url", "title"].forEach((key) => {
        const reference = normalizeKey(value[key]);
        if (reference) set.add(reference);
      });
      ["activities", "items", "results"].forEach((key) => {
        if (key in value) collectHighlightReferences(value[key], set);
      });
    }
  }

  function getHighlightSet(category) {
    const set = new Set();
    const highlights = state.highlights;
    if (!highlights) return set;
    if (Array.isArray(highlights)) {
      if (category === "featured") collectHighlightReferences(highlights, set);
      return set;
    }
    if (typeof highlights !== "object") return set;
    const aliases = {
      featured: ["featured", "highlights", "priority", "重點"],
      "high-return": ["high_return", "high-return", "highReturn", "高回饋"],
      upcoming: ["upcoming", "starting_soon", "即將開始"],
      ending: ["ending", "ending_soon", "即將結束"],
      "sold-out": ["sold_out", "partial_sold_out", "額滿"]
    };
    (aliases[category] || []).forEach((key) => {
      if (key in highlights) collectHighlightReferences(highlights[key], set);
    });
    return set;
  }

  function isExplicitHighlight(activity, category) {
    const references = getHighlightSet(category);
    if (!references.size) return false;
    return [activity.id, activity.external_id, activity.url, activity.title]
      .map(normalizeKey)
      .filter(Boolean)
      .some((reference) => references.has(reference));
  }

  function isHighReturn(activity) {
    if (isExplicitHighlight(activity, "high-return")) return true;
    if (activity.is_high_return === true || nestedBoolean(activity.insights, ["high_return", "is_high_return"])) {
      return true;
    }
    const text = activitySearchText(activity);
    if (text.includes("高回饋")) return true;
    const percentages = [...text.matchAll(/(\d+(?:\.\d+)?)\s*%/g)].map((match) => Number(match[1]));
    return percentages.some((value) => value >= 10);
  }

  function daysFromToday(value) {
    const date = parseDate(value);
    if (!date) return Number.POSITIVE_INFINITY;
    return Math.ceil((date.getTime() - startOfToday().getTime()) / DAY_MS);
  }

  function isUpcoming(activity) {
    if (isExplicitHighlight(activity, "upcoming")) return true;
    if (activity.insights && activity.insights.is_upcoming === true) return true;
    const days = daysFromToday(activity.start_date);
    return days >= 1 && days <= UPCOMING_DAYS;
  }

  function isEnding(activity) {
    if (isExplicitHighlight(activity, "ending")) return true;
    if (normalizeKey(activity.lifecycle) === "ended") return false;
    const days = daysFromToday(activity.end_date);
    return days >= 0 && days <= ENDING_DAYS;
  }

  function temporalBadgeConfig(activity) {
    const lifecycle = normalizeKey(activity.lifecycle);
    if (lifecycle === "upcoming") {
      const days = daysFromToday(activity.start_date);
      if (days === 0) return { label: "今日開始", className: "is-upcoming" };
      if (days === 1) return { label: "明日開始", className: "is-upcoming" };
      if (Number.isFinite(days) && days > 1) return { label: `${days} 天後開始`, className: "is-upcoming" };
    }
    if (isEnding(activity)) {
      const days = daysFromToday(activity.end_date);
      if (days === 0) return { label: "今日截止", className: "is-ending" };
      if (days === 1) return { label: "明日截止", className: "is-ending" };
      return { label: `剩 ${days} 天`, className: "is-ending" };
    }
    return lifecycleLabels[lifecycle] || lifecycleLabels.unknown;
  }

  function isSoldOut(activity) {
    const status = normalizeKey(activity.quota_status);
    return isExplicitHighlight(activity, "sold-out") || ["sold_out", "partial_sold_out"].includes(status);
  }

  function isFeatured(activity) {
    const explicitHighlights = getHighlightSet("featured");
    if (explicitHighlights.size) return isExplicitHighlight(activity, "featured");
    return isHighReturn(activity) ||
      isUpcoming(activity) ||
      isEnding(activity) ||
      normalizeKey(activity.quota_status) === "partial_sold_out" ||
      nestedBoolean(activity.insights, ["featured", "highlight", "priority"]);
  }

  function matchesCategory(activity) {
    switch (state.category) {
      case "high-return": return isHighReturn(activity);
      case "upcoming": return isUpcoming(activity);
      case "ending": return isEnding(activity);
      case "sold-out": return isSoldOut(activity);
      case "all": return true;
      default: return isFeatured(activity);
    }
  }

  function scoreActivity(activity) {
    let score = 0;
    if (isExplicitHighlight(activity, "featured")) score += 60;
    if (isHighReturn(activity)) score += 35;
    if (isUpcoming(activity)) score += 22;
    if (isEnding(activity)) score += 18;
    if (normalizeKey(activity.quota_status) === "partial_sold_out") score += 10;
    if (normalizeKey(activity.quota_status) === "sold_out") score -= 25;
    return score;
  }

  function sortActivities(left, right) {
    if (state.category === "featured" || state.category === "high-return") {
      const scoreDifference = scoreActivity(right) - scoreActivity(left);
      if (scoreDifference) return scoreDifference;
    }
    const leftDate = parseDate(left.start_date)?.getTime() ?? Number.MAX_SAFE_INTEGER;
    const rightDate = parseDate(right.start_date)?.getTime() ?? Number.MAX_SAFE_INTEGER;
    if (state.category === "ending") {
      const leftEnd = parseDate(left.end_date)?.getTime() ?? Number.MAX_SAFE_INTEGER;
      const rightEnd = parseDate(right.end_date)?.getTime() ?? Number.MAX_SAFE_INTEGER;
      if (leftEnd !== rightEnd) return leftEnd - rightEnd;
    }
    if (leftDate !== rightDate) return leftDate - rightDate;
    return collator.compare(String(left.title ?? ""), String(right.title ?? ""));
  }

  function filteredActivities() {
    const query = state.query.toLocaleLowerCase("zh-Hant");
    return state.activities.filter((activity) => {
      if (state.provider && String(activity.provider_name ?? "") !== state.provider) return false;
      if (query && !activitySearchText(activity).includes(query)) return false;
      return matchesCategory(activity);
    }).sort(sortActivities);
  }

  function createBadge(status, map) {
    const config = map[normalizeKey(status)] || map.unknown;
    return createConfiguredBadge(config);
  }

  function createConfiguredBadge(config) {
    const badge = document.createElement("span");
    badge.className = `badge ${config.className}`.trim();
    badge.textContent = config.label;
    return badge;
  }

  function rewardCallout(activity) {
    const insights = activity.insights;
    if (!insights || typeof insights !== "object" || Array.isArray(insights)) return null;
    const percent = Number(insights.max_reward_percent);
    if (Number.isFinite(percent) && percent > 0) {
      return `${percent.toLocaleString("zh-TW", { maximumFractionDigits: 2 })}%`;
    }
    const amount = Number(insights.fixed_reward_amount);
    if (Number.isFinite(amount) && amount > 0) {
      return `$${amount.toLocaleString("zh-TW", { maximumFractionDigits: 2 })}`;
    }
    return null;
  }

  function appendTextParagraphs(container, values) {
    values.forEach((value) => {
      const paragraph = document.createElement("p");
      paragraph.textContent = value;
      container.append(paragraph);
    });
  }

  function safeExternalUrl(value) {
    try {
      const url = new URL(String(value));
      return ["https:", "http:"].includes(url.protocol) ? url.href : "";
    } catch {
      return "";
    }
  }

  function safeCalendarUrl(value) {
    try {
      const url = new URL(String(value));
      const valid = url.protocol === "https:"
        && url.hostname === "calendar.google.com"
        && url.pathname === "/calendar/render"
        && url.searchParams.get("action") === "TEMPLATE";
      return valid ? url.href : "";
    } catch {
      return "";
    }
  }

  function renderActivity(activity) {
    const fragment = elements.cardTemplate.content.cloneNode(true);
    const card = fragment.querySelector(".activity-card");
    const provider = fragment.querySelector(".provider-name");
    const badges = fragment.querySelector(".status-badges");
    const title = fragment.querySelector(".activity-title");
    const period = fragment.querySelector(".activity-period");
    const reward = fragment.querySelector(".reward-callout");
    const rewardValue = fragment.querySelector(".reward-value");
    const insightChips = fragment.querySelector(".insight-chips");
    const conditionPreview = fragment.querySelector(".condition-preview");
    const insightDetail = fragment.querySelector(".insight-detail");
    const insightList = fragment.querySelector(".insight-list");
    const conditionDetail = fragment.querySelector(".condition-detail");
    const conditionText = fragment.querySelector(".condition-text");
    const calendarLink = fragment.querySelector(".calendar-link");
    const officialLink = fragment.querySelector(".official-link");

    const insights = normalizeInsights(activity);
    const insightTags = normalizeInsightTags(activity);
    const conditions = normalizeConditions(activity);

    provider.textContent = stringValue(activity.provider_name) || "支付業者待確認";
    title.textContent = stringValue(activity.title) || "未命名活動";
    period.textContent = formatPeriod(activity);
    badges.append(createConfiguredBadge(temporalBadgeConfig(activity)));
    badges.append(createBadge(activity.quota_status, quotaLabels));

    const rewardText = rewardCallout(activity);
    if (rewardText) {
      reward.hidden = false;
      rewardValue.textContent = rewardText;
      reward.setAttribute("aria-label", `最高回饋 ${rewardText}`);
    }

    insightTags.slice(0, 3).forEach((value) => {
      const item = document.createElement("li");
      item.textContent = value;
      insightChips.append(item);
    });

    conditionPreview.textContent = stringValue(activity.editorial_summary) || conditions[0] || "詳細資格與支付條件請參考官方活動頁。";

    if (insights.length) {
      insights.forEach((value) => {
        const item = document.createElement("li");
        item.textContent = value;
        insightList.append(item);
      });
    } else {
      insightDetail.hidden = true;
    }

    if (conditions.length) {
      appendTextParagraphs(conditionText, conditions);
    } else {
      conditionDetail.hidden = true;
    }

    const calendarUrl = safeCalendarUrl(activity.google_calendar_url);
    if (calendarUrl) {
      calendarLink.href = calendarUrl;
      calendarLink.setAttribute("aria-label", `${title.textContent}－加入 Google 行事曆（另開新視窗）`);
    } else {
      calendarLink.hidden = true;
    }

    const officialUrl = safeExternalUrl(activity.url);
    if (officialUrl) {
      officialLink.href = officialUrl;
      officialLink.setAttribute("aria-label", `${title.textContent}－查看官方活動頁（另開新視窗）`);
    } else {
      officialLink.hidden = true;
    }

    const status = normalizeKey(activity.quota_status);
    if (status === "sold_out") card.classList.add("is-sold-out");
    if (isEnding(activity)) card.classList.add("is-ending");
    if (isUpcoming(activity)) card.classList.add("is-upcoming");
    return fragment;
  }

  function updateFilterControls() {
    elements.categoryTabs.querySelectorAll("button[data-category]").forEach((button) => {
      const active = button.dataset.category === state.category;
      button.setAttribute("aria-pressed", String(active));
    });
    elements.clearFilters.hidden = !state.query && !state.provider && state.category === "featured";
    elements.resultsContext.textContent = categoryLabels[state.category] || categoryLabels.featured;
  }

  function renderActivities() {
    const activities = filteredActivities();
    const fragment = document.createDocumentFragment();
    activities.forEach((activity) => fragment.append(renderActivity(activity)));
    elements.activityList.replaceChildren(fragment);
    elements.activityList.setAttribute("aria-busy", "false");
    elements.resultsCount.textContent = `共 ${activities.length} 項`;
    elements.activityList.hidden = activities.length === 0;
    elements.emptyState.hidden = activities.length !== 0;
    elements.errorState.hidden = true;
    updateFilterControls();
  }

  function resetFilters(showAll = false) {
    state.query = "";
    state.provider = "";
    state.category = showAll ? "all" : "featured";
    elements.searchInput.value = "";
    elements.providerSelect.value = "";
    renderActivities();
  }

  function populateProviders() {
    const providers = uniqueStrings([
      ...state.activities.map((activity) => stringValue(activity.provider_name)),
      ...state.providerCoverage.map((provider) => stringValue(provider.provider_name))
    ])
      .sort(collator.compare);
    const defaultOption = elements.providerSelect.querySelector("option[value='']");
    elements.providerSelect.replaceChildren(defaultOption);
    const options = document.createDocumentFragment();
    providers.forEach((provider) => {
      const option = document.createElement("option");
      option.value = provider;
      option.textContent = provider;
      options.append(option);
    });
    elements.providerSelect.append(options);
  }

  const discoveryCoverageLabels = {
    complete: "活動公開發現完整",
    full: "活動公開發現完整",
    official_api: "官方 API",
    official_listing: "官方活動列表",
    partial: "活動公開發現部分涵蓋",
    limited: "活動公開發現部分涵蓋",
    ai_review: "活動列表由 AI 補查",
    unavailable: "活動來源暫時無法讀取",
    unknown: "活動發現狀態待確認"
  };

  const publicStatusCoverageLabels = {
    complete: "額滿狀態公開涵蓋",
    full: "額滿狀態公開涵蓋",
    public: "額滿狀態可公開查證",
    partial: "額滿狀態部分公開",
    app_only: "額滿狀態需至 App 確認",
    unavailable: "額滿來源暫時無法讀取",
    unknown: "額滿狀態待確認"
  };

  function coverageLabel(value, labels, fallback) {
    const text = stringValue(value);
    if (!text) return fallback;
    const normalized = normalizeKey(text);
    if (labels[normalized]) return labels[normalized];
    if (/[㐀-鿿]/u.test(text)) return text;
    return text.replaceAll("_", " ");
  }

  function coverageTone(value) {
    const normalized = normalizeKey(value);
    if (["fail", "failed", "error", "unavailable"].includes(normalized)) return "is-error";
    if (/partial|unknown|app|review|limited|部分|未知|缺口|無公開|無法公開/i.test(normalized)) {
      return "is-warning";
    }
    return "is-complete";
  }

  function renderProviderCoverage() {
    elements.providerCoverageList.replaceChildren();
    if (!state.providerCoverage.length) {
      elements.coverageSection.hidden = true;
      return;
    }

    const fragment = document.createDocumentFragment();
    let activityCount = 0;
    state.providerCoverage.forEach((provider) => {
      const name = stringValue(provider.provider_name) || "支付業者待確認";
      const countValue = Number(provider.activity_count);
      const count = Number.isFinite(countValue) && countValue >= 0 ? Math.trunc(countValue) : 0;
      activityCount += count;

      const item = document.createElement("li");
      item.className = "coverage-item";
      const heading = document.createElement("div");
      heading.className = "coverage-provider";
      const providerName = document.createElement("strong");
      providerName.textContent = name;
      const providerCount = document.createElement("span");
      providerCount.textContent = `${count} 項有效活動`;
      heading.append(providerName, providerCount);

      const statuses = document.createElement("div");
      statuses.className = "coverage-statuses";
      const discoveryStatus = document.createElement("span");
      discoveryStatus.className = `coverage-badge ${coverageTone(provider.discovery_status)}`;
      discoveryStatus.textContent = coverageLabel(
        provider.discovery_status,
        discoveryCoverageLabels,
        "活動發現狀態待確認"
      );
      const publicStatus = document.createElement("span");
      const rawPublicStatus = stringValue(provider.public_status_coverage);
      const explicitCoverageNote = stringValue(provider.coverage_note);
      const normalizedPublicStatus = normalizeKey(rawPublicStatus);
      const hasStructuredPublicStatus = normalizedPublicStatus in publicStatusCoverageLabels;
      const fallbackCoverageNote = !explicitCoverageNote && !hasStructuredPublicStatus
        ? rawPublicStatus
        : "";
      const coverageNote = explicitCoverageNote || fallbackCoverageNote;
      const publicStatusValue = hasStructuredPublicStatus ? normalizedPublicStatus : "unknown";
      publicStatus.className = `coverage-badge ${coverageTone(publicStatusValue)}`;
      publicStatus.textContent = coverageLabel(
        publicStatusValue,
        publicStatusCoverageLabels,
        "額滿狀態待確認"
      );
      statuses.append(discoveryStatus, publicStatus);
      const officialMetric = requestMetric(provider.official_sources);
      const extendedMetric = requestMetric(provider.extended_checks);
      if (officialMetric || extendedMetric) {
        const health = document.createElement("p");
        health.className = "coverage-health";
        const values = [];
        if (officialMetric) {
          values.push(`官方入口成功 ${officialMetric.succeeded}/${officialMetric.expected}`);
        }
        if (extendedMetric) {
          values.push(`延伸檢查成功 ${extendedMetric.succeeded}/${extendedMetric.expected}`);
        }
        health.textContent = values.join("；");
        statuses.append(health);
      }
      if (coverageNote) {
        const note = document.createElement("p");
        note.className = `coverage-note ${coverageTone(publicStatusValue)}`;
        note.textContent = coverageNote;
        statuses.append(note);
      }
      item.append(heading, statuses);
      fragment.append(item);
    });

    elements.providerCoverageList.append(fragment);
    elements.coverageSummary.textContent = `${state.providerCoverage.length} 家業者 · ${activityCount} 項活動`;
    elements.coverageSection.hidden = false;
  }

  function renderSummary() {
    const activeActivities = state.activities.filter((activity) => normalizeKey(activity.lifecycle) !== "ended");
    const values = [
      ["有效活動", activeActivities.length],
      ["高回饋", activeActivities.filter(isHighReturn).length],
      ["即將開始", activeActivities.filter(isUpcoming).length],
      ["額滿提醒", activeActivities.filter(isSoldOut).length]
    ];
    const fragment = document.createDocumentFragment();
    values.forEach(([label, value]) => {
      const wrapper = document.createElement("div");
      wrapper.className = "summary-item";
      const term = document.createElement("dt");
      const description = document.createElement("dd");
      term.textContent = label;
      description.textContent = String(value);
      wrapper.append(term, description);
      fragment.append(wrapper);
    });
    elements.summaryList.replaceChildren(fragment);
  }

  function renderGeneratedAt(value) {
    const date = parseDate(value);
    elements.updatedAt.textContent = date
      ? `最近更新：${dateTimeFormatter.format(date)}`
      : "每日更新一次";
  }

  function requestMetric(value) {
    if (!value || typeof value !== "object") return null;
    if (!("expected" in value) && !("succeeded" in value)) return null;
    const expected = Number(value.expected);
    const succeeded = Number(value.succeeded);
    if (!Number.isFinite(expected) || !Number.isFinite(succeeded)) return null;
    return {
      expected: Math.max(0, Math.trunc(expected)),
      succeeded: Math.max(0, Math.trunc(succeeded))
    };
  }

  function summarizeSourceHealth(sourceHealth) {
    const appOnlyCount = state.activities.filter((activity) => normalizeKey(activity.quota_status) === "unknown_app_only").length;
    let status = "ok";
    let failureCount = 0;
    let hasGroupedSummary = false;

    if (typeof sourceHealth === "string") {
      status = normalizeKey(sourceHealth);
    } else if (sourceHealth && typeof sourceHealth === "object") {
      status = normalizeKey(sourceHealth.status || sourceHealth.state || "ok");
      const officialMetric = requestMetric(sourceHealth.official_sources);
      const extendedMetric = requestMetric(sourceHealth.extended_checks);
      if (officialMetric || extendedMetric) {
        hasGroupedSummary = true;
        const values = [];
        if (officialMetric) {
          values.push(`官方入口成功 ${officialMetric.succeeded}/${officialMetric.expected}`);
          failureCount += Math.max(0, officialMetric.expected - officialMetric.succeeded);
        }
        if (extendedMetric) {
          values.push(`延伸檢查成功 ${extendedMetric.succeeded}/${extendedMetric.expected}`);
          failureCount += Math.max(0, extendedMetric.expected - extendedMetric.succeeded);
        }
        elements.sourceHealthText.textContent = values.join("・");
      }
      if (Array.isArray(sourceHealth.failures)) {
        failureCount = Math.max(failureCount, sourceHealth.failures.length);
      }
      if (Array.isArray(sourceHealth.failed_sources)) failureCount = Math.max(failureCount, sourceHealth.failed_sources.length);
      failureCount = Math.max(
        failureCount,
        Number(sourceHealth.failed_count ?? sourceHealth.failure_count ?? 0) || 0
      );
      const total = Number(sourceHealth.total ?? sourceHealth.total_sources);
      const success = Number(sourceHealth.success ?? sourceHealth.success_count);
      if (Number.isFinite(total) && Number.isFinite(success)) failureCount = Math.max(failureCount, total - success);
    }

    elements.sourceHealth.classList.remove("is-warning", "is-error");
    if (["failed", "error", "unavailable"].includes(status)) {
      elements.sourceHealth.classList.add("is-error");
      if (!hasGroupedSummary) {
        elements.sourceHealthText.textContent = "資料更新異常";
      }
    } else if (failureCount > 0 || ["partial", "warning", "degraded"].includes(status)) {
      elements.sourceHealth.classList.add("is-warning");
      if (!hasGroupedSummary) {
        elements.sourceHealthText.textContent = "部分官網需補查";
      }
    } else if (appOnlyCount > 0) {
      elements.sourceHealth.classList.add("is-warning");
      if (!hasGroupedSummary) {
        elements.sourceHealthText.textContent = `資料更新正常・${appOnlyCount} 項需至 App 確認`;
      }
    } else {
      if (!hasGroupedSummary) {
        elements.sourceHealthText.textContent = "資料更新正常";
      }
    }
  }

  function validatePayload(payload) {
    if (!payload || typeof payload !== "object" || !Array.isArray(payload.activities)) {
      throw new Error("資料格式不正確");
    }
    return payload;
  }

  async function loadData() {
    elements.activityList.hidden = false;
    elements.activityList.setAttribute("aria-busy", "true");
    elements.emptyState.hidden = true;
    elements.errorState.hidden = true;
    elements.resultsCount.textContent = "載入中";

    try {
      const response = await fetch(DATA_URL, { cache: "no-cache" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = validatePayload(await response.json());
      state.activities = payload.activities.filter((activity) => activity && typeof activity === "object");
      state.highlights = payload.highlights;
      state.providerCoverage = Array.isArray(payload.provider_coverage)
        ? payload.provider_coverage.filter((provider) => provider && typeof provider === "object")
        : [];
      elements.dailyHeadline.textContent = stringValue(payload.headline) || "今天值得留意的支付優惠";
      renderGeneratedAt(payload.generated_at);
      summarizeSourceHealth(payload.source_health);
      populateProviders();
      renderSummary(payload.summary);
      renderProviderCoverage();
      renderActivities();
    } catch (error) {
      elements.activityList.replaceChildren();
      elements.activityList.hidden = true;
      elements.activityList.setAttribute("aria-busy", "false");
      elements.errorState.hidden = false;
      elements.resultsCount.textContent = "載入失敗";
      elements.errorMessage.textContent = "優惠資料暫時無法取得，請重新載入或稍後再試。";
      console.error("Failed to load promotions data", error);
    }
  }

  elements.searchInput.addEventListener("input", (event) => {
    state.query = event.currentTarget.value.trim();
    renderActivities();
  });

  elements.providerSelect.addEventListener("change", (event) => {
    state.provider = event.currentTarget.value;
    renderActivities();
  });

  elements.categoryTabs.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-category]");
    if (!button) return;
    state.category = button.dataset.category;
    renderActivities();
  });

  elements.clearFilters.addEventListener("click", () => resetFilters(false));
  elements.emptyClear.addEventListener("click", () => resetFilters(true));
  elements.retryButton.addEventListener("click", loadData);
  elements.themeToggle.addEventListener("click", cycleTheme);

  const handleSystemThemeChange = () => {
    if (currentThemePreference() === "system") applyTheme("system");
  };
  if (typeof systemThemeQuery.addEventListener === "function") {
    systemThemeQuery.addEventListener("change", handleSystemThemeChange);
  } else if (typeof systemThemeQuery.addListener === "function") {
    systemThemeQuery.addListener(handleSystemThemeChange);
  }

  applyTheme(currentThemePreference());
  loadData();
})();
