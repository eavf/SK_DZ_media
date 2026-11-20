-- Migration 004: AI návrh relevancie k Slovensku / vzťahom SK–DZ (ai_relevance.py)
-- Spusti raz na DB (produkcia cez phpMyAdmin — dz_app nemá ALTER; lokál: mysql -u root dz_news < …)
--
--   ai_relevance        — návrh AI: 1 = relevantné, 0 = nerelevantné (NULL = ešte neposúdené)
--   ai_relevance_conf   — istota modelu 0–100
--   ai_relevance_reason — krátke zdôvodnenie modelu
--   ai_relevance_at     — kedy bol článok posúdený; každý článok sa posiela do AI len raz
--
-- Je to len NÁVRH: relevance ani deleted_at sa nemenia, rozhoduje človek.
-- Kým migrácia nie je spustená, aplikácia posudzovanie preskočí.

ALTER TABLE `articles`
  ADD COLUMN IF NOT EXISTS `ai_relevance`        tinyint(4)   DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS `ai_relevance_conf`   tinyint(4)   DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS `ai_relevance_reason` varchar(255) DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS `ai_relevance_at`     datetime     DEFAULT NULL,
  ADD INDEX IF NOT EXISTS `idx_articles_ai_relevance` (`ai_relevance`);
