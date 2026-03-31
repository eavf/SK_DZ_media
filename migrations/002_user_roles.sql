-- Migration 002: rozšírenie rolí používateľov
-- Spusti raz na DB (produkcia aj lokál):
--   mysql -u root dz_news < migrations/002_user_roles.sql
--
-- Povolené hodnoty role: 'user', 'power', 'admin'
--
--   user  — môže spúšťať pipeline (search, ingest, extract, translate)
--   power — user + môže modifikovať záznamy článkov (label, delete, export, ...)
--   admin — power + správa zdrojov a používateľov
--
-- Tabuľka users existuje od migrácie 001. Táto migrácia len pridáva CHECK constraint.
-- Na MariaDB 10.2+ je CHECK podporovaný.

ALTER TABLE `users`
  MODIFY COLUMN `role` varchar(16) NOT NULL DEFAULT 'user',
  ADD CONSTRAINT `chk_users_role` CHECK (`role` IN ('user', 'power', 'admin'));
