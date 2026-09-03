CREATE OR ALTER PROCEDURE dbo.p_demo
    @n        INT,
    @label    NVARCHAR(50) = N'demo',
    @result   INT          OUTPUT,
    @err      NVARCHAR(MAX) OUTPUT
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @i INT = 0;
    DECLARE @total INT = 0;

    BEGIN TRY
        IF @n < 0
        BEGIN
            RAISERROR(N'p_demo: negative @n is not supported.', 16, 1);
        END;

        IF @n > 0
        BEGIN
            DECLARE @bonus INT = 100;
            SET @total = @bonus;
        END
        ELSE
        BEGIN
            SET @total = -1;
        END;

        WHILE @i < 3
        BEGIN
            SET @i = @i + 1;
            SET @total = @total + @i;
        END;

        SET @total = @total + ISNULL(@bonus, -999);
        SET @result = @total;
    END TRY
    BEGIN CATCH
        SET @result = -1;
        SET @err = CONCAT(N'Error in p_demo: ', ERROR_MESSAGE());
    END CATCH
END;
GO
