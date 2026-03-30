-- Migration 001: users table pre autentifikáciu
-- Spusti raz na DB (produkcia aj lokál):
--   mysql -u root dz_news < migrations/001_add_users.sql

CREATE TABLE IF NOT EXISTS `users` (
  `id`            int(11)      NOT NULL AUTO_INCREMENT,
  `username`      varchar(64)  NOT NULL,
  `password_hash` varchar(255) NOT NULL,
  `role`          varchar(16)  NOT NULL DEFAULT 'user',
  `created_at`    datetime     NOT NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;