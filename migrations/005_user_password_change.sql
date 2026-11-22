-- Migration 005: zmena hesla používateľom a vynútená zmena hesla adminom
-- Spusti raz na DB (produkcia cez phpMyAdmin — dz_app nemá ALTER; lokál: mysql -u root dz_news < …)
--
--   must_change_password — 1 = po prihlásení musí používateľ zmeniť heslo (nastaví admin v Používatelia)
--   password_changed_at  — kedy si používateľ naposledy zmenil heslo
--
-- Bez migrácie funguje zmena vlastného hesla, vynútenie zmeny adminom je nedostupné.

ALTER TABLE `users`
  ADD COLUMN IF NOT EXISTS `must_change_password` tinyint(1) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS `password_changed_at`  datetime   DEFAULT NULL;
