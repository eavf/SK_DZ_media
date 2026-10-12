-- Migration 003: AI klasifikácia článkov (športové spravodajstvo)
-- Spusti raz na DB (produkcia aj lokál):
--   mysql -u root dz_news < migrations/003_ai_classification.sql
--
--   ai_label      — výsledok AI klasifikácie: 'sport' | 'other' (NULL = ešte neklasifikované)
--   ai_checked_at — kedy bol článok klasifikovaný; každý článok sa posiela do AI len raz
--
-- Článok s ai_label='sport' je soft-deleted (deleted_at) a relevance=0.
-- Riadok sa nemaže natvrdo — url_hash zabráni opätovnému ingestu a klasifikácii.

ALTER TABLE `articles`
  ADD COLUMN IF NOT EXISTS `ai_label`      varchar(20) DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS `ai_checked_at` datetime    DEFAULT NULL,
  ADD INDEX IF NOT EXISTS `idx_articles_ai_label` (`ai_label`);
