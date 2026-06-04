# Manual PostgreSQL Setup for Citadel Quant Bot

Since the automated setup is having issues, let's do this manually step by step.

## Step 1: Install PostgreSQL (if not already installed)

Download and install PostgreSQL 18 from:
https://www.postgresql.org/download/windows/

During installation:
- Choose password: `citadel123` (or remember your choice)
- Keep default port: 5432
- Install pgAdmin (optional, but helpful)

## Step 2: Start PostgreSQL Service

Open Command Prompt as Administrator and run:
```cmd
net start postgresql-x64-18
```

Or use Services.msc to start the PostgreSQL service.

## Step 3: Set Up Database

Open a new Command Prompt and navigate to PostgreSQL bin directory:
```cmd
cd "C:\Program Files\PostgreSQL\18\bin"
```

Create the database (replace 'your_password' with actual password):
```cmd
# Using default postgres database - no need to create separate database
```

If prompted for password, enter your PostgreSQL password.

## Step 4: Create Database Schema

In the same command prompt, create the schema:
```cmd
psql -U postgres -d postgres -f "C:\Users\user\Desktop\citadel_bot - 2.0\database_schema.sql"
```

## Step 5: Update Python Configuration

Edit `database_manager.py` and update the password:

```python
DB_CONFIG = {
    'host': 'localhost',
    'port': 5432,
    'user': 'postgres',
    'password': 'your_actual_password_here',  # ← Change this
    'database': 'postgres'
}
```

## Step 6: Test Connection

```cmd
cd "C:\Users\user\Desktop\citadel_bot - 2.0"
python simple_db_queries.py
```

## Alternative: Use pgAdmin (GUI Method)

1. Open pgAdmin
2. Connect to your PostgreSQL server
3. Use the default "postgres" database (already exists)
4. Right-click the new database → Query Tool
5. Copy and paste the contents of `database_schema.sql`
6. Click Execute (lightning bolt icon)

## Troubleshooting

### If psql command not found:
Add PostgreSQL bin to PATH:
```cmd
set PATH=%PATH%;"C:\Program Files\PostgreSQL\18\bin"
```

### If connection fails:
- Make sure PostgreSQL service is running
- Check if port 5432 is available (netstat -an | find "5432")
- Verify password in configuration

### If schema creation fails:
- Check that database_schema.sql exists
- Make sure you're connected to the postgres database
- Check for syntax errors in the schema file

## Quick Test Commands

Once set up, test with:

```bash
# Test basic connection
psql -U postgres -d postgres -c "SELECT version();"

# Check tables
psql -U postgres -d postgres -c "\dt"

# Check if data exists
psql -U postgres -d postgres -c "SELECT COUNT(*) FROM instruments;"
```

## Next Steps

After setup is complete:
1. Run migration: `python migrate_to_postgres.py`
2. Test queries: `python simple_db_queries.py`
3. Run analytics: `python database_analytics_simple.py`

The manual approach gives you full control over the setup process!