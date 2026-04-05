-- DZ News Monitor – inicializačná schéma
-- Spusti raz na novom serveri:
--   mysql -u root -p < migrations/000_init_schema.sql
--
-- Vytvára databázu dz_news a všetky tabuľky.
-- Ak databáza alebo tabuľky už existujú, nič neprepisuje (IF NOT EXISTS).

CREATE DATABASE IF NOT EXISTS `dz_news`
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE `dz_news`;

-- --------------------------------------------------------
-- sources
-- --------------------------------------------------------
CREATE TABLE IF NOT EXISTS `sources` (
  `id`           int(11)      NOT NULL AUTO_INCREMENT,
  `domain`       varchar(255) NOT NULL,
  `is_preferred` tinyint(1)   NOT NULL DEFAULT 0,
  `is_avoided`   tinyint(1)   NOT NULL DEFAULT 0,
  `notes`        varchar(255) DEFAULT NULL,
  `created_at`   datetime     NOT NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `domain` (`domain`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- --------------------------------------------------------
-- articles
-- --------------------------------------------------------
CREATE TABLE IF NOT EXISTS `articles` (
  `id`                   bigint(20)    NOT NULL AUTO_INCREMENT,
  `source_id`            int(11)       NOT NULL,
  `url`                  text          NOT NULL,
  `final_url`            text          DEFAULT NULL,
  `final_url_canonical`  varchar(1024) DEFAULT NULL,
  `final_url_hash`       char(64)      DEFAULT NULL,
  `url_canonical`        varchar(1024) NOT NULL,
  `url_hash`             char(64)      NOT NULL,
  `title`                text          DEFAULT NULL,
  `title_fr`             text          DEFAULT NULL,
  `normalized_title`     text          DEFAULT NULL,
  `title_hash`           char(64)      DEFAULT NULL,
  `published_at_text`    varchar(64)   DEFAULT NULL,
  `published_at_real`    datetime      DEFAULT NULL,
  `published_conf`       varchar(12)   DEFAULT NULL,
  `snippet`              text          DEFAULT NULL,
  `snippet_fr`           text          DEFAULT NULL,
  `language`             varchar(10)   DEFAULT NULL,
  `lang_detected`        varchar(10)   DEFAULT NULL,
  `extraction_ok`        tinyint(1)    NOT NULL DEFAULT 0,
  `source_label`         varchar(255)  DEFAULT NULL,
  `first_seen_at`        datetime      NOT NULL DEFAULT current_timestamp(),
  `last_seen_at`         datetime      NOT NULL DEFAULT current_timestamp(),
  `fetched_at`           datetime      DEFAULT NULL,
  `http_status`          int(11)       DEFAULT NULL,
  `fetch_error`          text          DEFAULT NULL,
  `content_text`         mediumtext    DEFAULT NULL,
  `content_hash`         char(64)      DEFAULT NULL,
  `ingestion_engine`     varchar(30)   DEFAULT NULL,
  `ingestion_query_id`   varchar(10)   DEFAULT NULL,
  `ingestion_rank`       int(11)       DEFAULT NULL,
  `relevance`            tinyint(4)    DEFAULT NULL,
  `relevance_note`       varchar(255)  DEFAULT NULL,
  `deleted_at`           datetime      DEFAULT NULL,
  `content_text_fr`      longtext      DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_articles_url_hash` (`url_hash`),
  KEY `idx_articles_source`            (`source_id`),
  KEY `idx_articles_last_seen`         (`last_seen_at`),
  KEY `idx_articles_extraction_ok`     (`extraction_ok`),
  KEY `idx_articles_published_at_real` (`published_at_real`),
  KEY `idx_articles_content_hash`      (`content_hash`),
  KEY `idx_articles_relevance`         (`relevance`),
  KEY `idx_articles_deleted_at`        (`deleted_at`),
  KEY `idx_articles_title_hash`        (`title_hash`),
  KEY `idx_articles_source_title_hash` (`source_id`, `title_hash`),
  CONSTRAINT `fk_articles_source` FOREIGN KEY (`source_id`) REFERENCES `sources` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- --------------------------------------------------------
-- runs
-- --------------------------------------------------------
CREATE TABLE IF NOT EXISTS `runs` (
  `id`                  bigint(20)   NOT NULL AUTO_INCREMENT,
  `started_at`          datetime     NOT NULL DEFAULT current_timestamp(),
  `engine`              varchar(30)  NOT NULL,
  `time_filter_query`   varchar(30)  NOT NULL,
  `window_start`        datetime     DEFAULT NULL,
  `window_end`          datetime     DEFAULT NULL,
  `window_type`         varchar(30)  DEFAULT NULL,
  `num`                 int(11)      NOT NULL,
  `hl`                  varchar(10)  DEFAULT NULL,
  `gl`                  varchar(10)  DEFAULT NULL,
  `sort`                varchar(20)  DEFAULT NULL,
  `fallback_triggered`  tinyint(1)   NOT NULL DEFAULT 0,
  `bundle_filename`     varchar(255) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- --------------------------------------------------------
-- run_articles
-- --------------------------------------------------------
CREATE TABLE IF NOT EXISTS `run_articles` (
  `run_id`     bigint(20)  NOT NULL,
  `article_id` bigint(20)  NOT NULL,
  `query_id`   varchar(10) DEFAULT NULL,
  PRIMARY KEY (`run_id`, `article_id`),
  KEY `idx_ra_article` (`article_id`),
  CONSTRAINT `fk_ra_run`     FOREIGN KEY (`run_id`)     REFERENCES `runs`     (`id`),
  CONSTRAINT `fk_ra_article` FOREIGN KEY (`article_id`) REFERENCES `articles` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- --------------------------------------------------------
-- users
-- --------------------------------------------------------
CREATE TABLE IF NOT EXISTS `users` (
  `id`            int(11)      NOT NULL AUTO_INCREMENT,
  `username`      varchar(64)  NOT NULL,
  `password_hash` varchar(255) NOT NULL,
  `role`          varchar(16)  NOT NULL DEFAULT 'user',
  `created_at`    datetime     NOT NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `username` (`username`),
  CONSTRAINT `chk_users_role` CHECK (`role` IN ('user', 'power', 'admin'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;