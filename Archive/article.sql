-- phpMyAdmin SQL Dump
-- version 5.2.2
-- https://www.phpmyadmin.net/
--
-- Host: mariadb:3306
-- Generation Time: Mar 27, 2026 at 12:54 PM
-- Server version: 11.4.6-MariaDB
-- PHP Version: 8.2.27

SET SQL_MODE = "NO_AUTO_VALUE_ON_ZERO";
START TRANSACTION;
SET time_zone = "+00:00";

--
-- Database: `dz_news`
--

-- --------------------------------------------------------

--
-- Table structure for table `articles`
--

CREATE TABLE `articles` (
  `id` bigint(20) NOT NULL,
  `source_id` int(11) NOT NULL,
  `url` text NOT NULL,
  `final_url` text DEFAULT NULL,
  `final_url_canonical` varchar(1024) DEFAULT NULL,
  `final_url_hash` char(64) DEFAULT NULL,
  `url_canonical` varchar(1024) NOT NULL,
  `url_hash` char(64) NOT NULL,
  `title` text DEFAULT NULL,
  `normalized_title` text DEFAULT NULL,
  `title_hash` char(64) DEFAULT NULL,
  `published_at_text` varchar(64) DEFAULT NULL,
  `published_at_real` datetime DEFAULT NULL,
  `published_conf` varchar(12) DEFAULT NULL,
  `snippet` text DEFAULT NULL,
  `language` varchar(10) DEFAULT NULL,
  `lang_detected` varchar(10) DEFAULT NULL,
  `extraction_ok` tinyint(1) NOT NULL DEFAULT 0,
  `source_label` varchar(255) DEFAULT NULL,
  `first_seen_at` datetime NOT NULL DEFAULT current_timestamp(),
  `last_seen_at` datetime NOT NULL DEFAULT current_timestamp(),
  `fetched_at` datetime DEFAULT NULL,
  `http_status` int(11) DEFAULT NULL,
  `fetch_error` text DEFAULT NULL,
  `content_text` mediumtext DEFAULT NULL,
  `content_hash` char(64) DEFAULT NULL,
  `ingestion_engine` varchar(30) DEFAULT NULL,
  `ingestion_query_id` varchar(10) DEFAULT NULL,
  `ingestion_rank` int(11) DEFAULT NULL,
  `relevance` tinyint(4) DEFAULT NULL,
  `relevance_note` varchar(255) DEFAULT NULL,
  `deleted_at` datetime DEFAULT NULL,
  `content_text_fr` longtext DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

--
-- Indexes for dumped tables
--

--
-- Indexes for table `articles`
--
ALTER TABLE `articles`
  ADD PRIMARY KEY (`id`),
  ADD UNIQUE KEY `uq_articles_url_hash` (`url_hash`),
  ADD KEY `idx_articles_source` (`source_id`),
  ADD KEY `idx_articles_last_seen` (`last_seen_at`),
  ADD KEY `idx_articles_extraction_ok` (`extraction_ok`),
  ADD KEY `idx_articles_published_at_real` (`published_at_real`),
  ADD KEY `idx_articles_content_hash` (`content_hash`),
  ADD KEY `idx_articles_relevance` (`relevance`),
  ADD KEY `idx_articles_deleted_at` (`deleted_at`),
  ADD KEY `idx_articles_title_hash` (`title_hash`),
  ADD KEY `idx_articles_source_title_hash` (`source_id`,`title_hash`);

--
-- AUTO_INCREMENT for dumped tables
--

--
-- AUTO_INCREMENT for table `articles`
--
ALTER TABLE `articles`
  MODIFY `id` bigint(20) NOT NULL AUTO_INCREMENT;

--
-- Constraints for dumped tables
--

--
-- Constraints for table `articles`
--
ALTER TABLE `articles`
  ADD CONSTRAINT `fk_articles_source` FOREIGN KEY (`source_id`) REFERENCES `sources` (`id`);
COMMIT;
