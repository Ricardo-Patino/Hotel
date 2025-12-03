-- MySQL dump 10.13  Distrib 8.0.41, for Win64 (x86_64)
--
-- Host: localhost    Database: hotel_villagrace
-- ------------------------------------------------------
-- Server version	8.0.41

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!50503 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

--
-- Table structure for table `asignaciondecision`
--

DROP TABLE IF EXISTS `asignaciondecision`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `asignaciondecision` (
  `Id_Decision` int NOT NULL AUTO_INCREMENT,
  `Codigo_Reserva` int NOT NULL,
  `Tipo` enum('Auto','Manual','Reasignacion') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'Auto',
  `Resultado` enum('OK','Fallo','Parcial') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'OK',
  `Score_Total` decimal(9,4) DEFAULT NULL,
  `Detalle_JSON` json DEFAULT NULL,
  `Ejecutada_Por` int DEFAULT NULL,
  `Ejecutada_En` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`Id_Decision`),
  KEY `IX_AD_Reserva` (`Codigo_Reserva`),
  KEY `IX_AD_Tipo` (`Tipo`),
  KEY `IX_AD_Resultado` (`Resultado`),
  CONSTRAINT `FK_AD_Reserva` FOREIGN KEY (`Codigo_Reserva`) REFERENCES `reserva` (`Codigo_Reserva`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=4 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `audit_log`
--

DROP TABLE IF EXISTS `audit_log`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `audit_log` (
  `Id` int NOT NULL AUTO_INCREMENT,
  `Fecha` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `Usuario` varchar(120) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Accion` varchar(60) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Detalles` json DEFAULT NULL,
  PRIMARY KEY (`Id`),
  KEY `IX_Audit_Accion` (`Accion`),
  KEY `IX_Audit_Usuario` (`Usuario`),
  KEY `IX_Audit_Fecha` (`Fecha`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `auditoria_log`
--

DROP TABLE IF EXISTS `auditoria_log`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `auditoria_log` (
  `Id` int NOT NULL AUTO_INCREMENT,
  `Usuario` varchar(120) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Entidad` varchar(60) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Entidad_Id` varchar(40) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Accion` varchar(60) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Datos` json DEFAULT NULL,
  `Fecha` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`Id`),
  KEY `IX_Auditoria_Entidad` (`Entidad`),
  KEY `IX_Auditoria_Accion` (`Accion`),
  KEY `IX_Auditoria_Fecha` (`Fecha`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!50003 SET @saved_cs_client      = @@character_set_client */ ;
/*!50003 SET @saved_cs_results     = @@character_set_results */ ;
/*!50003 SET @saved_col_connection = @@collation_connection */ ;
/*!50003 SET character_set_client  = utf8mb4 */ ;
/*!50003 SET character_set_results = utf8mb4 */ ;
/*!50003 SET collation_connection  = utf8mb4_0900_ai_ci */ ;
/*!50003 SET @saved_sql_mode       = @@sql_mode */ ;
/*!50003 SET sql_mode              = 'STRICT_ALL_TABLES' */ ;
DELIMITER ;;
/*!50003 CREATE*/ /*!50017 DEFINER=`root`@`localhost`*/ /*!50003 TRIGGER `trg_auditoria_log_after_ins` AFTER INSERT ON `auditoria_log` FOR EACH ROW BEGIN
  INSERT INTO Audit_Log (Fecha, Usuario, Accion, Detalles)
  VALUES (
    COALESCE(NEW.Fecha, NOW()),
    NEW.Usuario,
    CONCAT(NEW.Entidad, '.', LOWER(NEW.Accion)),
    JSON_OBJECT('Entidad', NEW.Entidad, 'Entidad_Id', NEW.Entidad_Id, 'Datos', NEW.Datos)
  );
END */;;
DELIMITER ;
/*!50003 SET sql_mode              = @saved_sql_mode */ ;
/*!50003 SET character_set_client  = @saved_cs_client */ ;
/*!50003 SET character_set_results = @saved_cs_results */ ;
/*!50003 SET collation_connection  = @saved_col_connection */ ;

--
-- Table structure for table `cliente`
--

DROP TABLE IF EXISTS `cliente`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `cliente` (
  `Codigo_Cliente` int NOT NULL AUTO_INCREMENT,
  `Cedula` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Nombre` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Apellido` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Telefono` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Correo` varchar(120) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Fecha_Nacimiento` date NOT NULL,
  `Fecha_modificacion` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`Codigo_Cliente`),
  KEY `IX_Cliente_Correo` (`Correo`),
  KEY `IX_Cliente_Cedula` (`Cedula`)
) ENGINE=InnoDB AUTO_INCREMENT=6 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `documento`
--

DROP TABLE IF EXISTS `documento`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `documento` (
  `Id` int NOT NULL AUTO_INCREMENT,
  `Tipo` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Ruta` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `MimeType` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'application/pdf',
  `TamanoBytes` int DEFAULT NULL,
  `Fecha_Creacion` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`Id`),
  KEY `IX_Documento_Tipo` (`Tipo`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `fin_cash_movement`
--

DROP TABLE IF EXISTS `fin_cash_movement`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `fin_cash_movement` (
  `id_move` int NOT NULL AUTO_INCREMENT,
  `session_id` int NOT NULL,
  `tipo` enum('Ingreso','Egreso','Ajuste') COLLATE utf8mb4_unicode_ci NOT NULL,
  `metodo` enum('Efectivo','Tarjeta','Transferencia','Otro') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'Efectivo',
  `concepto` varchar(160) COLLATE utf8mb4_unicode_ci NOT NULL,
  `referencia` varchar(60) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `monto` decimal(14,2) NOT NULL,
  `created_by` int NOT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `source` enum('Manual','POS','BackOffice') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'Manual',
  `link_receipt` int DEFAULT NULL,
  `link_invoice` int DEFAULT NULL,
  `link_note` int DEFAULT NULL,
  `estado` enum('Aplicado','Anulado') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'Aplicado',
  PRIMARY KEY (`id_move`),
  KEY `IX_fin_cash_movement_session` (`session_id`),
  CONSTRAINT `FK_fin_cash_movement_session` FOREIGN KEY (`session_id`) REFERENCES `fin_cash_session` (`id_session`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `fin_cash_session`
--

DROP TABLE IF EXISTS `fin_cash_session`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `fin_cash_session` (
  `id_session` int NOT NULL AUTO_INCREMENT,
  `fecha` date NOT NULL,
  `opened_by` int NOT NULL,
  `opening_cash` decimal(14,2) NOT NULL DEFAULT '0.00',
  `notes_open` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `status` enum('open','closed','reopened') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'open',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `closed_at` datetime DEFAULT NULL,
  `closed_by` int DEFAULT NULL,
  `closing_cash_counted` decimal(14,2) DEFAULT NULL,
  `notes_close` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  PRIMARY KEY (`id_session`),
  UNIQUE KEY `UQ_fin_cash_session_day` (`fecha`,`opened_by`),
  KEY `IX_fin_cash_session_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `fin_invoices`
--

DROP TABLE IF EXISTS `fin_invoices`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `fin_invoices` (
  `id_factura` int NOT NULL AUTO_INCREMENT,
  `numero` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL,
  `cliente_nombre` varchar(120) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `cliente_email` varchar(120) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `moneda` varchar(10) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'CRC',
  `monto_total` decimal(12,2) NOT NULL,
  `descripcion` text COLLATE utf8mb4_unicode_ci,
  `archivo_path` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `id_reserva` int DEFAULT NULL,
  `id_usuario` int NOT NULL,
  `fecha_emision` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `estado` enum('Borrador','Emitida','Pagada','Anulada') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'Emitida',
  PRIMARY KEY (`id_factura`),
  UNIQUE KEY `UQ_fin_invoices_numero` (`numero`),
  KEY `IX_fin_invoices_email` (`cliente_email`),
  KEY `IX_fin_invoices_estado` (`estado`),
  KEY `IX_fin_invoices_fecha` (`fecha_emision`),
  KEY `IX_fin_invoices_reserva` (`id_reserva`),
  KEY `IX_fin_invoices_usuario` (`id_usuario`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `fin_ledger_line`
--

DROP TABLE IF EXISTS `fin_ledger_line`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `fin_ledger_line` (
  `id_line` int NOT NULL AUTO_INCREMENT,
  `id_tx` int NOT NULL,
  `line_no` int NOT NULL,
  `account` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `debit` decimal(14,2) NOT NULL DEFAULT '0.00',
  `credit` decimal(14,2) NOT NULL DEFAULT '0.00',
  `description` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  PRIMARY KEY (`id_line`),
  UNIQUE KEY `UQ_fin_ledger_line` (`id_tx`,`line_no`),
  KEY `IX_fin_ledger_line_account` (`account`),
  CONSTRAINT `FK_fin_ledger_line_tx` FOREIGN KEY (`id_tx`) REFERENCES `fin_ledger_tx` (`id_tx`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `fin_ledger_tx`
--

DROP TABLE IF EXISTS `fin_ledger_tx`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `fin_ledger_tx` (
  `id_tx` int NOT NULL AUTO_INCREMENT,
  `external_id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `source` enum('POS','BACKOFFICE') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'POS',
  `reserva_id` int DEFAULT NULL,
  `currency` varchar(10) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'CRC',
  `total` decimal(14,2) NOT NULL DEFAULT '0.00',
  `status` enum('posted','voided') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'posted',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `meta` json DEFAULT NULL,
  PRIMARY KEY (`id_tx`),
  UNIQUE KEY `UQ_fin_ledger_tx_ext` (`external_id`),
  KEY `IX_fin_ledger_tx_reserva` (`reserva_id`),
  KEY `IX_fin_ledger_tx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `fin_monthly_close`
--

DROP TABLE IF EXISTS `fin_monthly_close`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `fin_monthly_close` (
  `id_close` int NOT NULL AUTO_INCREMENT,
  `period_key` char(7) NOT NULL,
  `totals_json` json NOT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `created_by` int DEFAULT NULL,
  `hash_sum` char(64) DEFAULT NULL,
  PRIMARY KEY (`id_close`),
  UNIQUE KEY `uq_fin_monthly_close` (`period_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `fin_notes`
--

DROP TABLE IF EXISTS `fin_notes`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `fin_notes` (
  `id_note` int NOT NULL AUTO_INCREMENT,
  `numero` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL,
  `tipo` enum('Credito','Debito') COLLATE utf8mb4_unicode_ci NOT NULL,
  `ref_invoice` int DEFAULT NULL,
  `ref_reserva` int DEFAULT NULL,
  `currency` varchar(10) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'CRC',
  `monto_abs` decimal(14,2) NOT NULL,
  `motivo` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `emitido_por` int NOT NULL,
  `creado_en` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `estado` enum('Emitida','Anulada') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'Emitida',
  PRIMARY KEY (`id_note`),
  UNIQUE KEY `UQ_fin_notes_numero` (`numero`),
  KEY `IX_fin_notes_invoice` (`ref_invoice`),
  KEY `IX_fin_notes_reserva` (`ref_reserva`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `fin_period_lock`
--

DROP TABLE IF EXISTS `fin_period_lock`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `fin_period_lock` (
  `id_lock` int NOT NULL AUTO_INCREMENT,
  `period_key` char(7) NOT NULL,
  `period_start` date NOT NULL,
  `period_end` date NOT NULL,
  `status` enum('closed','reopened') NOT NULL DEFAULT 'closed',
  `note` varchar(255) DEFAULT NULL,
  `closed_by` int DEFAULT NULL,
  `closed_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `reopened_by` int DEFAULT NULL,
  `reopened_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id_lock`),
  UNIQUE KEY `uq_fin_period_lock` (`period_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `fin_receipts`
--

DROP TABLE IF EXISTS `fin_receipts`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `fin_receipts` (
  `id_receipt` int NOT NULL AUTO_INCREMENT,
  `numero` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL,
  `reserva_id` int DEFAULT NULL,
  `invoice_id` int DEFAULT NULL,
  `tx_id` int DEFAULT NULL,
  `metodo` varchar(30) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'Tarjeta',
  `currency` varchar(10) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'CRC',
  `monto` decimal(14,2) NOT NULL,
  `emitido_por` int NOT NULL,
  `creado_en` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `estado` enum('Emitido','Anulado') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'Emitido',
  PRIMARY KEY (`id_receipt`),
  UNIQUE KEY `UQ_fin_receipts_numero` (`numero`),
  KEY `IX_fin_receipts_reserva` (`reserva_id`),
  KEY `IX_fin_receipts_invoice` (`invoice_id`),
  KEY `IX_fin_receipts_tx` (`tx_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `funcionario`
--

DROP TABLE IF EXISTS `funcionario`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `funcionario` (
  `Codigo_Funcionario` int NOT NULL AUTO_INCREMENT,
  `Cedula` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Nombre` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Apellido` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Puesto` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Fecha_Nacimiento` date NOT NULL,
  `Fecha_modificacion` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`Codigo_Funcionario`),
  KEY `IX_Funcionario_Cedula` (`Cedula`)
) ENGINE=InnoDB AUTO_INCREMENT=4 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `habitacion`
--

DROP TABLE IF EXISTS `habitacion`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `habitacion` (
  `Codigo_Habitacion` int NOT NULL AUTO_INCREMENT,
  `Numero_Habitacion` varchar(10) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Tipo` enum('Sencilla','Doble','Suite') COLLATE utf8mb4_unicode_ci NOT NULL,
  `Precio_Noche` decimal(12,2) NOT NULL,
  `Estado` enum('Disponible','Ocupada','Mantenimiento','Limpieza') COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Fecha_modificacion` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`Codigo_Habitacion`),
  UNIQUE KEY `UQ_Habitacion_Numero` (`Numero_Habitacion`),
  KEY `IX_Habitacion_Tipo` (`Tipo`),
  KEY `IX_Habitacion_Estado` (`Estado`)
) ENGINE=InnoDB AUTO_INCREMENT=9 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `housekeepingtask`
--

DROP TABLE IF EXISTS `housekeepingtask`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `housekeepingtask` (
  `Id` int NOT NULL AUTO_INCREMENT,
  `Habitacion_Id` int NOT NULL,
  `Estado` enum('Pendiente','En proceso','Terminado') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'Pendiente',
  `Observaciones` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Fecha_Creacion` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `Fecha_Cierre` datetime DEFAULT NULL,
  PRIMARY KEY (`Id`),
  KEY `FK_HK_Room` (`Habitacion_Id`),
  KEY `IX_HK_Estado` (`Estado`),
  KEY `IX_HK_Fecha` (`Fecha_Creacion`),
  CONSTRAINT `FK_HK_Room` FOREIGN KEY (`Habitacion_Id`) REFERENCES `habitacion` (`Codigo_Habitacion`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=8 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `invcategoria`
--

DROP TABLE IF EXISTS `invcategoria`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `invcategoria` (
  `Id` int NOT NULL AUTO_INCREMENT,
  `Nombre` varchar(80) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Descripcion` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Activa` tinyint(1) NOT NULL DEFAULT '1',
  `Fecha_Creacion` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `Fecha_Actualiza` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`Id`),
  UNIQUE KEY `UQ_InvCategoria_Nombre` (`Nombre`),
  KEY `IX_InvCategoria_Activa` (`Activa`)
) ENGINE=InnoDB AUTO_INCREMENT=6 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `invinsumo`
--

DROP TABLE IF EXISTS `invinsumo`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `invinsumo` (
  `Id` int NOT NULL AUTO_INCREMENT,
  `Categoria_Id` int NOT NULL,
  `Nombre` varchar(120) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Unidad` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Stock_Actual` decimal(12,3) NOT NULL DEFAULT '0.000',
  `Stock_Minimo` decimal(12,3) NOT NULL DEFAULT '0.000',
  `Activo` tinyint(1) NOT NULL DEFAULT '1',
  `Fecha_Creacion` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `Fecha_Actualiza` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`Id`),
  UNIQUE KEY `UQ_InvInsumo` (`Categoria_Id`,`Nombre`),
  KEY `IX_InvInsumo_Activo` (`Activo`),
  KEY `IX_InvInsumo_Categoria` (`Categoria_Id`),
  CONSTRAINT `FK_InvInsumo_Categoria` FOREIGN KEY (`Categoria_Id`) REFERENCES `invcategoria` (`Id`) ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=3 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `invmovimiento`
--

DROP TABLE IF EXISTS `invmovimiento`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `invmovimiento` (
  `Id` int NOT NULL AUTO_INCREMENT,
  `Insumo_Id` int NOT NULL,
  `Tipo` enum('AJUSTE','EDICION','INACTIVACION','REACTIVACION','ENTRADA_COMPRA','ENTRADA_DEVOLUCION') COLLATE utf8mb4_unicode_ci NOT NULL,
  `Campo` varchar(60) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Valor_Antes` varchar(120) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Valor_Despues` varchar(120) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Delta` decimal(14,3) DEFAULT NULL,
  `Motivo` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Doc_Tipo` varchar(30) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Doc_Numero` varchar(60) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Proveedor` varchar(120) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Fecha_Mov` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `Usuario_Id` int DEFAULT NULL,
  `Usuario_Nombre` varchar(120) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Usuario_Email` varchar(120) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  PRIMARY KEY (`Id`),
  KEY `IX_InvMovimiento_Insumo` (`Insumo_Id`),
  KEY `IX_InvMovimiento_Tipo` (`Tipo`),
  KEY `IX_InvMovimiento_Fecha` (`Fecha_Mov`),
  CONSTRAINT `FK_InvMovimiento_Insumo` FOREIGN KEY (`Insumo_Id`) REFERENCES `invinsumo` (`Id`) ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `kpi_stats`
--

DROP TABLE IF EXISTS `kpi_stats`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `kpi_stats` (
  `Id` int NOT NULL AUTO_INCREMENT,
  `Periodo` enum('day','week','month') COLLATE utf8mb4_unicode_ci NOT NULL,
  `Clave` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Total_Reservas` int NOT NULL DEFAULT '0',
  `Total_Monto` decimal(14,2) NOT NULL DEFAULT '0.00',
  `Fecha_Ultima` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`Id`),
  UNIQUE KEY `UQ_KPI` (`Periodo`,`Clave`),
  KEY `IX_KPI_Periodo` (`Periodo`),
  KEY `IX_KPI_Clave` (`Clave`)
) ENGINE=InnoDB AUTO_INCREMENT=4 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `maintenancerequest`
--

DROP TABLE IF EXISTS `maintenancerequest`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `maintenancerequest` (
  `Id` int NOT NULL AUTO_INCREMENT,
  `Habitacion_Id` int NOT NULL,
  `Titulo` varchar(120) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Descripcion` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Estado` enum('Pendiente','En progreso','Cerrado') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'Pendiente',
  `Fecha_Creacion` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `Fecha_Cierre` datetime DEFAULT NULL,
  PRIMARY KEY (`Id`),
  KEY `FK_MR_Room` (`Habitacion_Id`),
  KEY `IX_MR_Estado` (`Estado`),
  KEY `IX_MR_Fecha` (`Fecha_Creacion`),
  CONSTRAINT `FK_MR_Room` FOREIGN KEY (`Habitacion_Id`) REFERENCES `habitacion` (`Codigo_Habitacion`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `mantenimientocambioestado`
--

DROP TABLE IF EXISTS `mantenimientocambioestado`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mantenimientocambioestado` (
  `Id` int NOT NULL AUTO_INCREMENT,
  `Solicitud_Id` int NOT NULL,
  `Estado_Antes` varchar(12) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Estado_Despues` varchar(12) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Motivo` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Usuario_Id` int DEFAULT NULL,
  `Usuario_Nombre` varchar(120) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Fecha` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`Id`),
  KEY `IX_MantenimientoCambioEstado_Solicitud` (`Solicitud_Id`),
  KEY `IX_MantenimientoCambioEstado_Fecha` (`Fecha`),
  CONSTRAINT `FK_MantenimientoCambioEstado_Solicitud` FOREIGN KEY (`Solicitud_Id`) REFERENCES `mantenimientosolicitud` (`Id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `mantenimientonotificacion`
--

DROP TABLE IF EXISTS `mantenimientonotificacion`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mantenimientonotificacion` (
  `Id` int NOT NULL AUTO_INCREMENT,
  `Solicitud_Id` int DEFAULT NULL,
  `Para_Email` varchar(150) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Para_Nombre` varchar(120) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Asunto` varchar(150) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Cuerpo` text COLLATE utf8mb4_unicode_ci,
  `Enviada` tinyint(1) NOT NULL DEFAULT '0',
  `Fecha_Creacion` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `Fecha_Envio` datetime DEFAULT NULL,
  PRIMARY KEY (`Id`),
  KEY `IX_MantenimientoNotificacion_Solicitud` (`Solicitud_Id`),
  KEY `IX_MantenimientoNotificacion_Enviada` (`Enviada`),
  KEY `IX_MantenimientoNotificacion_Fecha` (`Fecha_Creacion`),
  CONSTRAINT `FK_MantenimientoNotificacion_Solicitud` FOREIGN KEY (`Solicitud_Id`) REFERENCES `mantenimientosolicitud` (`Id`) ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `mantenimientopreventivo`
--

DROP TABLE IF EXISTS `mantenimientopreventivo`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mantenimientopreventivo` (
  `Id` int NOT NULL AUTO_INCREMENT,
  `Equipo` varchar(120) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Ubicacion` varchar(120) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Descripcion` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Frecuencia_Dias` int NOT NULL,
  `Proxima_Fecha` date NOT NULL,
  `Activo` tinyint(1) NOT NULL DEFAULT '1',
  PRIMARY KEY (`Id`),
  KEY `IX_MantenimientoPreventivo_Activo` (`Activo`),
  KEY `IX_MantenimientoPreventivo_ProximaFecha` (`Proxima_Fecha`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `mantenimientosolicitud`
--

DROP TABLE IF EXISTS `mantenimientosolicitud`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mantenimientosolicitud` (
  `Id` int NOT NULL AUTO_INCREMENT,
  `Codigo_Habitacion` int NOT NULL,
  `Titulo` varchar(120) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Descripcion` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Prioridad` varchar(10) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'Media',
  `Estado` varchar(12) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'Abierta',
  `Fecha_Creacion` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `Fecha_Actualiza` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`Id`),
  KEY `IX_MantenimientoSolicitud_Habitacion` (`Codigo_Habitacion`),
  KEY `IX_MantenimientoSolicitud_Estado` (`Estado`),
  KEY `IX_MantenimientoSolicitud_Fecha` (`Fecha_Creacion`),
  CONSTRAINT `FK_MantenimientoSolicitud_Habitacion` FOREIGN KEY (`Codigo_Habitacion`) REFERENCES `habitacion` (`Codigo_Habitacion`) ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=3 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `preferenciahuesped`
--

DROP TABLE IF EXISTS `preferenciahuesped`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `preferenciahuesped` (
  `Id_Pref` int NOT NULL AUTO_INCREMENT,
  `Codigo_Cliente` int NOT NULL,
  `Clave` varchar(60) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Valor` varchar(120) COLLATE utf8mb4_unicode_ci NOT NULL,
  PRIMARY KEY (`Id_Pref`),
  KEY `IX_Pref_Cliente` (`Codigo_Cliente`),
  KEY `IX_Pref_Clave` (`Clave`),
  CONSTRAINT `FK_Pref_Cliente` FOREIGN KEY (`Codigo_Cliente`) REFERENCES `cliente` (`Codigo_Cliente`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=4 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `reglaasignacion`
--

DROP TABLE IF EXISTS `reglaasignacion`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `reglaasignacion` (
  `Id_Regla` int NOT NULL AUTO_INCREMENT,
  `Nombre` varchar(80) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Prioridad` int NOT NULL DEFAULT '100',
  `Peso` decimal(8,4) NOT NULL DEFAULT '1.0000',
  `Activa` tinyint(1) NOT NULL DEFAULT '1',
  `Criterio_JSON` json DEFAULT NULL,
  `Creada_Por` int DEFAULT NULL,
  `Fecha_Creacion` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`Id_Regla`),
  KEY `IX_Regla_Activa` (`Activa`,`Prioridad`)
) ENGINE=InnoDB AUTO_INCREMENT=4 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `reserva`
--

DROP TABLE IF EXISTS `reserva`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `reserva` (
  `Codigo_Reserva` int NOT NULL AUTO_INCREMENT,
  `Codigo_Cliente` int NOT NULL,
  `Codigo_Habitacion` int NOT NULL,
  `Habitacion_Id` int NOT NULL,
  `Codigo_Funcionario` int NOT NULL,
  `Fecha_Entrada` date NOT NULL,
  `Fecha_Salida` date NOT NULL,
  `Monto_Total` decimal(12,2) NOT NULL,
  `Estado` enum('Confirmada','Cancelada','Pendiente') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'Confirmada',
  `Canal` varchar(30) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Numero_Comprobante` varchar(40) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Fuente` varchar(30) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Politica_Cancelacion` varchar(200) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Observaciones` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Huespedes` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Descuento_Id` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Monto_Pagado` decimal(12,2) NOT NULL DEFAULT '0.00',
  `Fecha_Ultimo_Pago` datetime DEFAULT NULL,
  `Fecha_Registro` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`Codigo_Reserva`),
  UNIQUE KEY `UQ_Reserva_Comprobante` (`Numero_Comprobante`),
  KEY `IX_Reserva_Cliente` (`Codigo_Cliente`),
  KEY `IX_Reserva_Fechas` (`Fecha_Entrada`,`Fecha_Salida`),
  KEY `IX_Reserva_Estado` (`Estado`),
  KEY `IX_Reserva_MontoPagado` (`Monto_Pagado`),
  KEY `IX_Reserva_Habitacion_Id` (`Habitacion_Id`),
  KEY `FK_Reserva_Habitacion` (`Codigo_Habitacion`),
  KEY `FK_Reserva_Funcionario` (`Codigo_Funcionario`),
  CONSTRAINT `FK_Reserva_Cliente` FOREIGN KEY (`Codigo_Cliente`) REFERENCES `cliente` (`Codigo_Cliente`) ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT `FK_Reserva_Funcionario` FOREIGN KEY (`Codigo_Funcionario`) REFERENCES `funcionario` (`Codigo_Funcionario`) ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT `FK_Reserva_Habitacion` FOREIGN KEY (`Codigo_Habitacion`) REFERENCES `habitacion` (`Codigo_Habitacion`) ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=9 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!50003 SET @saved_cs_client      = @@character_set_client */ ;
/*!50003 SET @saved_cs_results     = @@character_set_results */ ;
/*!50003 SET @saved_col_connection = @@collation_connection */ ;
/*!50003 SET character_set_client  = utf8mb4 */ ;
/*!50003 SET character_set_results = utf8mb4 */ ;
/*!50003 SET collation_connection  = utf8mb4_0900_ai_ci */ ;
/*!50003 SET @saved_sql_mode       = @@sql_mode */ ;
/*!50003 SET sql_mode              = 'STRICT_ALL_TABLES' */ ;
DELIMITER ;;
/*!50003 CREATE*/ /*!50017 DEFINER=`root`@`localhost`*/ /*!50003 TRIGGER `trg_reserva_sync_before_ins` BEFORE INSERT ON `reserva` FOR EACH ROW BEGIN
  SET NEW.Habitacion_Id = NEW.Codigo_Habitacion;
END */;;
DELIMITER ;
/*!50003 SET sql_mode              = @saved_sql_mode */ ;
/*!50003 SET character_set_client  = @saved_cs_client */ ;
/*!50003 SET character_set_results = @saved_cs_results */ ;
/*!50003 SET collation_connection  = @saved_col_connection */ ;
/*!50003 SET @saved_cs_client      = @@character_set_client */ ;
/*!50003 SET @saved_cs_results     = @@character_set_results */ ;
/*!50003 SET @saved_col_connection = @@collation_connection */ ;
/*!50003 SET character_set_client  = utf8mb4 */ ;
/*!50003 SET character_set_results = utf8mb4 */ ;
/*!50003 SET collation_connection  = utf8mb4_0900_ai_ci */ ;
/*!50003 SET @saved_sql_mode       = @@sql_mode */ ;
/*!50003 SET sql_mode              = 'STRICT_ALL_TABLES' */ ;
DELIMITER ;;
/*!50003 CREATE*/ /*!50017 DEFINER=`root`@`localhost`*/ /*!50003 TRIGGER `trg_reserva_sync_before_upd` BEFORE UPDATE ON `reserva` FOR EACH ROW BEGIN
  IF NEW.Codigo_Habitacion <> OLD.Codigo_Habitacion THEN
    SET NEW.Habitacion_Id = NEW.Codigo_Habitacion;
  END IF;
END */;;
DELIMITER ;
/*!50003 SET sql_mode              = @saved_sql_mode */ ;
/*!50003 SET character_set_client  = @saved_cs_client */ ;
/*!50003 SET character_set_results = @saved_cs_results */ ;
/*!50003 SET collation_connection  = @saved_col_connection */ ;

--
-- Table structure for table `reservadocumento`
--

DROP TABLE IF EXISTS `reservadocumento`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `reservadocumento` (
  `Id` int NOT NULL AUTO_INCREMENT,
  `Codigo_Reserva` int NOT NULL,
  `Documento_Id` int NOT NULL,
  PRIMARY KEY (`Id`),
  KEY `IX_ResDoc_Reserva` (`Codigo_Reserva`),
  KEY `IX_ResDoc_Documento` (`Documento_Id`),
  CONSTRAINT `FK_ResDoc_Documento` FOREIGN KEY (`Documento_Id`) REFERENCES `documento` (`Id`) ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT `FK_ResDoc_Reserva` FOREIGN KEY (`Codigo_Reserva`) REFERENCES `reserva` (`Codigo_Reserva`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `reservaestancia`
--

DROP TABLE IF EXISTS `reservaestancia`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `reservaestancia` (
  `Id_Estancia` int NOT NULL AUTO_INCREMENT,
  `Codigo_Reserva` int NOT NULL,
  `Habitacion_Id` int NOT NULL,
  `Fecha_Desde` date NOT NULL,
  `Fecha_Hasta` date NOT NULL,
  `Origen` enum('Automatico','Manual','Reasignacion','Union','Division') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'Automatico',
  `Decision_Id` int DEFAULT NULL,
  `Score_Total` decimal(9,4) DEFAULT NULL,
  `Motivos_JSON` json DEFAULT NULL,
  `Preferencias_JSON` json DEFAULT NULL,
  `Estado` enum('Pendiente','Asignada','Liberada','Cancelada') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'Asignada',
  `Creado_Por` int DEFAULT NULL,
  `Creado_En` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`Id_Estancia`),
  KEY `IX_RE_Rango` (`Fecha_Desde`,`Fecha_Hasta`),
  KEY `IX_RE_Habitacion` (`Habitacion_Id`),
  KEY `IX_RE_Reserva` (`Codigo_Reserva`),
  KEY `IX_RE_Estado` (`Estado`),
  KEY `FK_RE_Decision` (`Decision_Id`),
  CONSTRAINT `FK_RE_Decision` FOREIGN KEY (`Decision_Id`) REFERENCES `asignaciondecision` (`Id_Decision`) ON DELETE SET NULL ON UPDATE CASCADE,
  CONSTRAINT `FK_RE_Habitacion` FOREIGN KEY (`Habitacion_Id`) REFERENCES `habitacion` (`Codigo_Habitacion`) ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT `FK_RE_Reserva` FOREIGN KEY (`Codigo_Reserva`) REFERENCES `reserva` (`Codigo_Reserva`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=3 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `rol`
--

DROP TABLE IF EXISTS `rol`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `rol` (
  `Codigo_Rol` int NOT NULL AUTO_INCREMENT,
  `Nombre` varchar(30) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Descripcion` varchar(200) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Estado` enum('Activo','Inactivo') COLLATE utf8mb4_unicode_ci DEFAULT 'Activo',
  `Fecha_Creacion` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `Fecha_Modificacion` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`Codigo_Rol`),
  UNIQUE KEY `Nombre` (`Nombre`)
) ENGINE=InnoDB AUTO_INCREMENT=5 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `usuario`
--

DROP TABLE IF EXISTS `usuario`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `usuario` (
  `Codigo_Usuario` int NOT NULL AUTO_INCREMENT,
  `Nombre` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Cedula_Pasaporte` varchar(40) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `Correo` varchar(120) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Telefono` varchar(25) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Contrasena` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Rol_Id` int DEFAULT NULL,
  `Estado` enum('Activo','Inactivo') COLLATE utf8mb4_unicode_ci DEFAULT 'Activo',
  `Fecha_Creacion` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `Fecha_Modificacion` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `Codigo_Cliente` int DEFAULT NULL,
  PRIMARY KEY (`Codigo_Usuario`),
  UNIQUE KEY `Correo` (`Correo`),
  UNIQUE KEY `Cedula_Pasaporte` (`Cedula_Pasaporte`),
  KEY `IX_Usuario_Rol` (`Rol_Id`),
  KEY `IX_Usuario_Cli` (`Codigo_Cliente`),
  CONSTRAINT `FK_Usuario_Cliente` FOREIGN KEY (`Codigo_Cliente`) REFERENCES `cliente` (`Codigo_Cliente`) ON DELETE SET NULL ON UPDATE CASCADE,
  CONSTRAINT `FK_Usuario_Rol` FOREIGN KEY (`Rol_Id`) REFERENCES `rol` (`Codigo_Rol`) ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=3 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Temporary view structure for view `v_fin_caja_resumen`
--

DROP TABLE IF EXISTS `v_fin_caja_resumen`;
/*!50001 DROP VIEW IF EXISTS `v_fin_caja_resumen`*/;
SET @saved_cs_client     = @@character_set_client;
/*!50503 SET character_set_client = utf8mb4 */;
/*!50001 CREATE VIEW `v_fin_caja_resumen` AS SELECT 
 1 AS `id_session`,
 1 AS `fecha`,
 1 AS `opening_cash`,
 1 AS `ingresos_efectivo`,
 1 AS `egresos_efectivo`,
 1 AS `ajustes_mas`,
 1 AS `ajustes_menos`,
 1 AS `efectivo_esperado`,
 1 AS `efectivo_contado`,
 1 AS `descuadre`*/;
SET character_set_client = @saved_cs_client;

--
-- Temporary view structure for view `v_reserva_pagos_pos`
--

DROP TABLE IF EXISTS `v_reserva_pagos_pos`;
/*!50001 DROP VIEW IF EXISTS `v_reserva_pagos_pos`*/;
SET @saved_cs_client     = @@character_set_client;
/*!50503 SET character_set_client = utf8mb4 */;
/*!50001 CREATE VIEW `v_reserva_pagos_pos` AS SELECT 
 1 AS `reserva_id`,
 1 AS `total_pagado_pos`*/;
SET character_set_client = @saved_cs_client;

--
-- Temporary view structure for view `v_reservas_ext`
--

DROP TABLE IF EXISTS `v_reservas_ext`;
/*!50001 DROP VIEW IF EXISTS `v_reservas_ext`*/;
SET @saved_cs_client     = @@character_set_client;
/*!50503 SET character_set_client = utf8mb4 */;
/*!50001 CREATE VIEW `v_reservas_ext` AS SELECT 
 1 AS `Codigo_Reserva`,
 1 AS `Numero_Comprobante`,
 1 AS `Estado`,
 1 AS `Canal`,
 1 AS `Fecha_Entrada`,
 1 AS `Fecha_Salida`,
 1 AS `Monto_Total`,
 1 AS `Fecha_Registro`,
 1 AS `Observaciones`,
 1 AS `Huespedes`,
 1 AS `Codigo_Cliente`,
 1 AS `ClienteCorreo`,
 1 AS `ClienteNombre`,
 1 AS `Codigo_Habitacion`,
 1 AS `Numero_Habitacion`,
 1 AS `Tipo_Habitacion`,
 1 AS `Precio_Noche`*/;
SET character_set_client = @saved_cs_client;

--
-- Table structure for table `villa`
--

DROP TABLE IF EXISTS `villa`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `villa` (
  `Id` int NOT NULL AUTO_INCREMENT,
  `Nombre` varchar(120) COLLATE utf8mb4_unicode_ci NOT NULL,
  `Capacidad` int NOT NULL DEFAULT '2',
  `TarifaBase` decimal(12,2) NOT NULL DEFAULT '0.00',
  `Estado` enum('Activo','Inactivo') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'Activo',
  PRIMARY KEY (`Id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Final view structure for view `v_fin_caja_resumen`
--

/*!50001 DROP VIEW IF EXISTS `v_fin_caja_resumen`*/;
/*!50001 SET @saved_cs_client          = @@character_set_client */;
/*!50001 SET @saved_cs_results         = @@character_set_results */;
/*!50001 SET @saved_col_connection     = @@collation_connection */;
/*!50001 SET character_set_client      = utf8mb4 */;
/*!50001 SET character_set_results     = utf8mb4 */;
/*!50001 SET collation_connection      = utf8mb4_0900_ai_ci */;
/*!50001 CREATE ALGORITHM=UNDEFINED */
/*!50013 DEFINER=`root`@`localhost` SQL SECURITY DEFINER */
/*!50001 VIEW `v_fin_caja_resumen` AS select `s`.`id_session` AS `id_session`,`s`.`fecha` AS `fecha`,`s`.`opening_cash` AS `opening_cash`,ifnull(`r`.`ingresos_efectivo`,0.00) AS `ingresos_efectivo`,ifnull(`m`.`egresos_efectivo`,0.00) AS `egresos_efectivo`,ifnull(`m`.`ajustes_mas`,0.00) AS `ajustes_mas`,ifnull(`m`.`ajustes_menos`,0.00) AS `ajustes_menos`,((((`s`.`opening_cash` + ifnull(`r`.`ingresos_efectivo`,0)) - ifnull(`m`.`egresos_efectivo`,0)) + ifnull(`m`.`ajustes_mas`,0)) - ifnull(`m`.`ajustes_menos`,0)) AS `efectivo_esperado`,`s`.`closing_cash_counted` AS `efectivo_contado`,(ifnull(`s`.`closing_cash_counted`,0) - ((((`s`.`opening_cash` + ifnull(`r`.`ingresos_efectivo`,0)) - ifnull(`m`.`egresos_efectivo`,0)) + ifnull(`m`.`ajustes_mas`,0)) - ifnull(`m`.`ajustes_menos`,0))) AS `descuadre` from ((`fin_cash_session` `s` left join (select cast(`fin_receipts`.`creado_en` as date) AS `fecha`,sum(`fin_receipts`.`monto`) AS `ingresos_efectivo` from `fin_receipts` where ((`fin_receipts`.`estado` = 'Emitido') and (`fin_receipts`.`metodo` = 'Efectivo')) group by cast(`fin_receipts`.`creado_en` as date)) `r` on((`r`.`fecha` = `s`.`fecha`))) left join (select cast(`fin_cash_movement`.`created_at` as date) AS `fecha`,sum((case when ((`fin_cash_movement`.`tipo` = 'Egreso') and (`fin_cash_movement`.`metodo` = 'Efectivo') and (`fin_cash_movement`.`estado` = 'Aplicado')) then `fin_cash_movement`.`monto` else 0 end)) AS `egresos_efectivo`,sum((case when ((`fin_cash_movement`.`metodo` = 'Efectivo') and (`fin_cash_movement`.`estado` = 'Aplicado') and (`fin_cash_movement`.`monto` >= 0) and (`fin_cash_movement`.`tipo` in ('Ingreso','Ajuste'))) then `fin_cash_movement`.`monto` else 0 end)) AS `ajustes_mas`,sum((case when ((`fin_cash_movement`.`metodo` = 'Efectivo') and (`fin_cash_movement`.`estado` = 'Aplicado') and (`fin_cash_movement`.`monto` < 0) and (`fin_cash_movement`.`tipo` = 'Ajuste')) then -(`fin_cash_movement`.`monto`) else 0 end)) AS `ajustes_menos` from `fin_cash_movement` group by cast(`fin_cash_movement`.`created_at` as date)) `m` on((`m`.`fecha` = `s`.`fecha`))) */;
/*!50001 SET character_set_client      = @saved_cs_client */;
/*!50001 SET character_set_results     = @saved_cs_results */;
/*!50001 SET collation_connection      = @saved_col_connection */;

--
-- Final view structure for view `v_reserva_pagos_pos`
--

/*!50001 DROP VIEW IF EXISTS `v_reserva_pagos_pos`*/;
/*!50001 SET @saved_cs_client          = @@character_set_client */;
/*!50001 SET @saved_cs_results         = @@character_set_results */;
/*!50001 SET @saved_col_connection     = @@collation_connection */;
/*!50001 SET character_set_client      = utf8mb4 */;
/*!50001 SET character_set_results     = utf8mb4 */;
/*!50001 SET collation_connection      = utf8mb4_0900_ai_ci */;
/*!50001 CREATE ALGORITHM=UNDEFINED */
/*!50013 DEFINER=`root`@`localhost` SQL SECURITY DEFINER */
/*!50001 VIEW `v_reserva_pagos_pos` AS select `t`.`reserva_id` AS `reserva_id`,sum((case when (`l`.`debit` > 0) then `l`.`debit` else 0 end)) AS `total_pagado_pos` from (`fin_ledger_tx` `t` join `fin_ledger_line` `l` on((`l`.`id_tx` = `t`.`id_tx`))) where (`t`.`status` = 'posted') group by `t`.`reserva_id` */;
/*!50001 SET character_set_client      = @saved_cs_client */;
/*!50001 SET character_set_results     = @saved_cs_results */;
/*!50001 SET collation_connection      = @saved_col_connection */;

--
-- Final view structure for view `v_reservas_ext`
--

/*!50001 DROP VIEW IF EXISTS `v_reservas_ext`*/;
/*!50001 SET @saved_cs_client          = @@character_set_client */;
/*!50001 SET @saved_cs_results         = @@character_set_results */;
/*!50001 SET @saved_col_connection     = @@collation_connection */;
/*!50001 SET character_set_client      = utf8mb4 */;
/*!50001 SET character_set_results     = utf8mb4 */;
/*!50001 SET collation_connection      = utf8mb4_0900_ai_ci */;
/*!50001 CREATE ALGORITHM=UNDEFINED */
/*!50013 DEFINER=`root`@`localhost` SQL SECURITY DEFINER */
/*!50001 VIEW `v_reservas_ext` AS select `r`.`Codigo_Reserva` AS `Codigo_Reserva`,`r`.`Numero_Comprobante` AS `Numero_Comprobante`,`r`.`Estado` AS `Estado`,`r`.`Canal` AS `Canal`,`r`.`Fecha_Entrada` AS `Fecha_Entrada`,`r`.`Fecha_Salida` AS `Fecha_Salida`,`r`.`Monto_Total` AS `Monto_Total`,`r`.`Fecha_Registro` AS `Fecha_Registro`,`r`.`Observaciones` AS `Observaciones`,`r`.`Huespedes` AS `Huespedes`,`c`.`Codigo_Cliente` AS `Codigo_Cliente`,`c`.`Correo` AS `ClienteCorreo`,concat(`c`.`Nombre`,' ',`c`.`Apellido`) AS `ClienteNombre`,`h`.`Codigo_Habitacion` AS `Codigo_Habitacion`,`h`.`Numero_Habitacion` AS `Numero_Habitacion`,`h`.`Tipo` AS `Tipo_Habitacion`,`h`.`Precio_Noche` AS `Precio_Noche` from ((`reserva` `r` join `cliente` `c` on((`r`.`Codigo_Cliente` = `c`.`Codigo_Cliente`))) join `habitacion` `h` on((`r`.`Codigo_Habitacion` = `h`.`Codigo_Habitacion`))) */;
/*!50001 SET character_set_client      = @saved_cs_client */;
/*!50001 SET character_set_results     = @saved_cs_results */;
/*!50001 SET collation_connection      = @saved_col_connection */;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

-- Dump completed on 2025-10-31  6:55:15
