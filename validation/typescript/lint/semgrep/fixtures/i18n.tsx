declare const t: (key: string) => string;

// ruleid: i18n-hardcoded-loading-caption
const caption = <div>Loading…</div>;
// ok: i18n-hardcoded-loading-caption
const translated = <div>{t('common.loading')}</div>;

// ruleid: i18n-placeholder-from-catalogue
const literal = <input placeholder="Search users" />;
// ok: i18n-placeholder-from-catalogue
const fromCatalogue = <input placeholder={t('users.search_placeholder')} />;

export { caption, translated, literal, fromCatalogue };
