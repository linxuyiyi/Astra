import i18n from "i18next";
import LanguageDetector from "i18next-browser-languagedetector";
import { initReactI18next } from "react-i18next";

import enTranslations from "../i18n/locales/en.json";
import zhTranslations from "../i18n/locales/zh.json";

// 与上游 v2.0.10 的 i18n/index.ts 等价，只是语言来源固定为 zh，并关闭 localStorage
// 缓存：Nexus 是中文产品，不应把语言偏好写进与设置页 iframe 共享的 localStorage。
i18n.use(LanguageDetector).use(initReactI18next).init({
  resources: {
    en: { translation: enTranslations },
    zh: { translation: zhTranslations },
  },
  lng: "zh",
  fallbackLng: "zh",
  interpolation: { escapeValue: false },
  detection: { order: ["navigator"], caches: [] },
});

export default i18n;
