-- Stand-ins for the SyteLine database objects the function uses (specs/008 R6). Run as `sa` by edge/sqlcontrol.py; the
-- placeholders @SQL_DB@, @SQL_USER@ and @SQL_PASS@ are filled in from the run's generated values. Idempotent.
-- The numbering routines here are STAND-INS: they hand out increasing numbers in the shape the function reads. SyteLine's
-- real routines do much more; that difference is listed in docs/MOCK_ENVIRONMENT.md (fidelity gaps).
IF DB_ID('@SQL_DB@') IS NULL CREATE DATABASE [@SQL_DB@];
GO
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = '@SQL_USER@')
    CREATE LOGIN [@SQL_USER@] WITH PASSWORD = '@SQL_PASS@', CHECK_POLICY = OFF;
ELSE
    ALTER LOGIN [@SQL_USER@] WITH PASSWORD = '@SQL_PASS@';
ALTER LOGIN [@SQL_USER@] ENABLE;
GO
USE [@SQL_DB@];
GO
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = '@SQL_USER@') CREATE USER [@SQL_USER@] FOR LOGIN [@SQL_USER@];
ALTER ROLE db_datareader ADD MEMBER [@SQL_USER@];
GRANT EXECUTE TO [@SQL_USER@];
GO
IF TYPE_ID('LongList') IS NULL CREATE TYPE dbo.LongList FROM nvarchar(4000) NULL;
IF TYPE_ID('Infobar') IS NULL CREATE TYPE dbo.Infobar FROM nvarchar(2800) NULL;
GO
IF OBJECT_ID('dbo.mock_flags') IS NULL CREATE TABLE dbo.mock_flags (name nvarchar(40) PRIMARY KEY, value nvarchar(100) NULL);
IF OBJECT_ID('dbo.mock_seq') IS NULL CREATE TABLE dbo.mock_seq (name nvarchar(10) PRIMARY KEY, next_val int NOT NULL);
IF OBJECT_ID('dbo.custaddr') IS NULL CREATE TABLE dbo.custaddr (
    cust_num nvarchar(7) NOT NULL, cust_seq int NOT NULL, name nvarchar(60) NULL,
    [addr##1] nvarchar(50) NULL, [addr##2] nvarchar(50) NULL, [addr##3] nvarchar(50) NULL, [addr##4] nvarchar(50) NULL,
    city nvarchar(30) NULL, state nvarchar(5) NULL, zip nvarchar(10) NULL, country nvarchar(30) NULL,
    PRIMARY KEY (cust_num, cust_seq));
IF OBJECT_ID('dbo.co') IS NULL CREATE TABLE dbo.co (
    co_num nvarchar(10) NOT NULL PRIMARY KEY, cust_num nvarchar(7) NULL, cust_seq int NULL,
    terms_code nvarchar(10) NULL, ship_code nvarchar(10) NULL);
GO
-- common behaviour of the two stand-in routines: the faults of the mock control API, then the next number of a sequence
CREATE OR ALTER PROCEDURE dbo.NextCoSp @Prefix nvarchar(10) = NULL, @Suffix nvarchar(10) = NULL, @Site int = NULL,
    @Key LongList OUTPUT, @Infobar Infobar OUTPUT
AS
BEGIN
    DECLARE @slow int = ISNULL((SELECT TRY_CAST(value AS int) FROM dbo.mock_flags WHERE name = 'slow_ms'), 0);
    IF @slow > 0 BEGIN DECLARE @d1 char(8) = CONVERT(char(8), DATEADD(second, (@slow + 999) / 1000, 0), 108); WAITFOR DELAY @d1; END
    IF EXISTS (SELECT 1 FROM dbo.mock_flags WHERE name = 'routine_error')
    BEGIN SET @Infobar = 'mock routine error'; RAISERROR('mock NextCoSp error', 16, 1); RETURN 16; END
    DECLARE @n int;
    UPDATE dbo.mock_seq SET @n = next_val, next_val = next_val + 1 WHERE name = 'co';
    SET @Key = CAST(@n AS nvarchar(20)); SET @Infobar = NULL;
END;
GO
CREATE OR ALTER PROCEDURE dbo.NextWOSp @Prefix nvarchar(10) = NULL, @Suffix nvarchar(10) = NULL, @Site int = NULL,
    @Key LongList OUTPUT, @Infobar Infobar OUTPUT
AS
BEGIN
    DECLARE @slow int = ISNULL((SELECT TRY_CAST(value AS int) FROM dbo.mock_flags WHERE name = 'slow_ms'), 0);
    IF @slow > 0 BEGIN DECLARE @d1 char(8) = CONVERT(char(8), DATEADD(second, (@slow + 999) / 1000, 0), 108); WAITFOR DELAY @d1; END
    IF EXISTS (SELECT 1 FROM dbo.mock_flags WHERE name = 'routine_error')
    BEGIN SET @Infobar = 'mock routine error'; RAISERROR('mock NextWOSp error', 16, 1); RETURN 16; END
    DECLARE @n int;
    UPDATE dbo.mock_seq SET @n = next_val, next_val = next_val + 1 WHERE name = 'job';
    SET @Key = CAST(@n AS nvarchar(20)); SET @Infobar = NULL;
END;
GO
IF NOT EXISTS (SELECT 1 FROM dbo.mock_seq WHERE name = 'co') INSERT dbo.mock_seq VALUES ('co', 100);
IF NOT EXISTS (SELECT 1 FROM dbo.mock_seq WHERE name = 'job') INSERT dbo.mock_seq VALUES ('job', 5000);
GO
