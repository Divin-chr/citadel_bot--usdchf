#!/usr/bin/env python3
"""
PostgreSQL Setup Checker for Citadel Quant Bot
Diagnoses and fixes PostgreSQL connection issues
"""

import subprocess
import os
import sys
from pathlib import Path

def run_command(cmd, shell=True):
    """Run a command and return result"""
    try:
        result = subprocess.run(cmd, shell=shell, capture_output=True, text=True)
        return result.returncode, result.stdout, result.stderr
    except Exception as e:
        return -1, "", str(e)

def check_postgresql_service():
    """Check if PostgreSQL service is running"""
    print("1. Checking PostgreSQL Service...")

    # Check Windows service
    returncode, stdout, stderr = run_command('sc query postgresql-x64-18')
    if returncode == 0 and 'RUNNING' in stdout:
        print("   PostgreSQL service is running")
        return True
    else:
        print("   PostgreSQL service is not running")

        # Try to start it
        print("   Attempting to start PostgreSQL service...")
        start_code, start_out, start_err = run_command('net start postgresql-x64-18')
        if start_code == 0:
            print("   PostgreSQL service started successfully")
            return True
        else:
            print(f"   Failed to start service: {start_err}")
            return False

def check_psql_connection(password=None):
    """Test psql connection"""
    print("2. Testing psql Connection...")

    psql_path = r"C:\Program Files\PostgreSQL\18\bin\psql.exe"
    env = os.environ.copy()
    if password:
        env['PGPASSWORD'] = password

    test_cmd = [psql_path, "-U", "postgres", "-c", "SELECT version();"]
    returncode, stdout, stderr = run_command(test_cmd, shell=False)

    if returncode == 0:
        print("   psql connection successful")
        return True
    else:
        print(f"   psql connection failed: {stderr.strip()}")
        return False

def setup_postgres_user():
    """Set up postgres user password"""
    print("3. Setting up PostgreSQL User...")

    psql_path = r"C:\Program Files\PostgreSQL\18\bin\psql.exe"

    # First try with no password (trust authentication)
    if check_psql_connection():
        # Set a password for postgres user
        print("   Setting password for postgres user...")
        alter_cmd = [psql_path, "-U", "postgres", "-c", "ALTER USER postgres PASSWORD 'citadel123';"]
        returncode, stdout, stderr = run_command(alter_cmd, shell=False)

        if returncode == 0:
            print("   Password set for postgres user: 'postgres'")
            return 'postgres'
        else:
            print(f"   Failed to set password: {stderr}")
            return None
    else:
        print("   Cannot connect to set password. Please check PostgreSQL installation.")
        return None

def check_database(password):
    """Check if postgres database is accessible"""

    psql_path = r"C:\Program Files\PostgreSQL\18\bin\psql.exe"
    env = os.environ.copy()
    env['PGPASSWORD'] = password

    # Test connection to postgres database
    test_cmd = [psql_path, "-U", "postgres", "-d", "postgres", "-c", "SELECT version();"]
    returncode, stdout, stderr = run_command(test_cmd, shell=False)

    if returncode == 0:
        print("   Connected to postgres database successfully")
        return True
    else:
        print(f"   Failed to connect to postgres database: {stderr}")
        return False

def create_schema(password):
    """Create database schema"""
    print("5. Creating Database Schema...")

    schema_file = Path("database_schema.sql")
    if not schema_file.exists():
        print("   database_schema.sql not found!")
        return False

    psql_path = r"C:\Program Files\PostgreSQL\18\bin\psql.exe"
    env = os.environ.copy()
    env['PGPASSWORD'] = password

    # Read schema file and execute
    with open(schema_file, 'r', encoding='utf-8') as f:
        schema_sql = f.read()

    # Execute schema
    schema_cmd = [psql_path, "-U", "postgres", "-d", "postgres", "-c", schema_sql]
    returncode, stdout, stderr = run_command(schema_cmd, shell=False)

    if returncode == 0:
        print("   Database schema created successfully")
        return True
    else:
        print(f"   Failed to create schema: {stderr}")
        return False

def update_config_file(password):
    """Update database_manager.py with correct password"""
    print("6. Updating Configuration...")

    config_file = Path("citadel_bot/database/database_manager.py")
    if not config_file.exists():
        print("   database_manager.py not found")
        return False

    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            content = f.read()

        # Update password
        old_config = f"'password': '{password}'" if f"'password': ''" in content else None
        if old_config:
            new_content = content.replace(f"'password': ''", f"'password': '{password}'")
        else:
            new_content = content.replace("'password': '',", f"'password': '{password}',")

        with open(config_file, 'w', encoding='utf-8') as f:
            f.write(new_content)

        print("   Configuration updated with database password")
        return True

    except Exception as e:
        print(f"   Failed to update config: {e}")
        return False

def test_database_connection(password):
    """Test full database connection"""
    print("7. Testing Database Connection...")

    psql_path = r"C:\Program Files\PostgreSQL\18\bin\psql.exe"
    env = os.environ.copy()
    env['PGPASSWORD'] = password

    test_cmd = [psql_path, "-U", "postgres", "-d", "postgres", "-c", "SELECT COUNT(*) FROM instruments;"]
    returncode, stdout, stderr = run_command(test_cmd, shell=False)

    if returncode == 0:
        print("   Database connection test successful")
        return True
    else:
        print(f"   Database connection test failed: {stderr}")
        return False

def main():
    """Main setup function"""
    print("Citadel Quant Bot - PostgreSQL Setup Checker")
    print("=" * 50)

    # Step 1: Check service
    if not check_postgresql_service():
        print("\nPostgreSQL service issue. Please ensure PostgreSQL is installed and running.")
        print("   Download from: https://www.postgresql.org/download/windows/")
        return False

    # Step 2: Setup user and get password
    password = setup_postgres_user()
    if not password:
        print("\nCould not set up PostgreSQL user. Please check installation.")
        return False

    # Step 3: Check database access
    if not check_database(password):
        print("\nCould not access postgres database.")
        return False

    # Step 4: Create schema
    if not create_schema(password):
        print("\nCould not create schema.")
        return False

    # Step 5: Update config
    if not update_config_file(password):
        print("\nCould not update configuration.")
        return False

    # Step 6: Test connection
    if not test_database_connection(password):
        print("\nDatabase setup incomplete.")
        return False

    print("\n" + "=" * 50)
    print("PostgreSQL setup complete!")
    print("\nNext steps:")
    print("1. Run migration: python migrate_to_postgres.py")
    print("2. Test queries: python simple_db_queries.py")
    print("3. Run analytics: python database_analytics_simple.py")
    print("\nDatabase password set to: citadel123")
    print("You can change this in database_manager.py if needed.")

    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)