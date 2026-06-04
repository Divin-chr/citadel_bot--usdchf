#!/usr/bin/env python3
"""
Database Setup Script for Citadel Quant Bot
Creates and initializes the PostgreSQL database
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

def run_psql_command(command: str, password: str = None):
    """Run a psql command"""
    psql_path = r"C:\Program Files\PostgreSQL\18\bin\psql.exe"

    env = os.environ.copy()
    if password:
        env['PGPASSWORD'] = password

    try:
        result = subprocess.run(
            [psql_path, "-U", "postgres", "-c", command],
            capture_output=True,
            text=True,
            env=env
        )
        return result.returncode == 0, result.stdout, result.stderr
    except Exception as e:
        return False, "", str(e)

def setup_database():
    """Set up the PostgreSQL database"""
    print("Citadel Quant Bot - Database Setup")
    print("=" * 40)

    # Try different password configurations
    passwords_to_try = [None, "", "postgres", "password","12345"]

    password = None
    for pwd in passwords_to_try:
        print(f"\nTrying password: {'(none)' if pwd is None else repr(pwd)}")
        success, stdout, stderr = run_psql_command("SELECT version();", pwd)
        if success:
            password = pwd
            print("✅ PostgreSQL connection successful")
            break
        else:
            print(f"❌ Connection failed: {stderr.strip()}")

    if password is None:
        print("\n❌ Could not connect to PostgreSQL. Please ensure:")
        print("1. PostgreSQL is installed and running")
        print("2. User 'postgres' exists")
        print("3. Update database_manager.py with correct credentials")
        return False

    # 1. Create database
    print("\n1. Creating database...")
    # Using default postgres database - no need to create
    if success:
        print("✅ Using postgres database")
    else:
        if "already exists" in stderr:
            print("ℹ️  Using existing postgres database")
        else:
            print(f"❌ Failed to create database: {stderr}")
            return False

    # 2. Create schema
    print("\n2. Creating database schema...")
    schema_file = Path("database_schema.sql")
    if not schema_file.exists():
        print("❌ database_schema.sql not found!")
        return False

    psql_path = r"C:\Program Files\PostgreSQL\18\bin\psql.exe"
    env = os.environ.copy()
    if password:
        env['PGPASSWORD'] = password

    try:
        with open(schema_file, 'r', encoding='utf-8') as f:
            schema_sql = f.read()

        result = subprocess.run(
            [psql_path, "-U", "postgres", "-d", "postgres", "-c", schema_sql],
            capture_output=True,
            text=True,
            env=env
        )

        if result.returncode == 0:
            print("✅ Database schema created successfully")
        else:
            print(f"❌ Failed to create schema: {result.stderr}")
            return False

    except Exception as e:
        print(f"❌ Error creating schema: {e}")
        return False

    # 3. Update database config
    print("\n3. Updating database configuration...")
    config_lines = []
    config_file = Path("citadel_bot/database/database_manager.py")

    if config_file.exists():
        with open(config_file, 'r', encoding='utf-8') as f:
            config_lines = f.readlines()

        # Update password in config
        updated = False
        for i, line in enumerate(config_lines):
            if "'password':" in line:
                config_lines[i] = f"            'password': '{password}',\n"
                updated = True
                break

        if updated:
            with open(config_file, 'w', encoding='utf-8') as f:
                f.writelines(config_lines)
            print("✅ Database configuration updated")
        else:
            print("⚠️  Could not update database configuration automatically")
    else:
        print("⚠️  database_manager.py not found, please update password manually")

    print("\n✅ Database setup complete!")
    print("\nNext steps:")
    print("1. Run migration: python migrate_to_postgres.py")
    print("2. Test queries: python simple_db_queries.py")
    print("3. Start the bot: python main.py")

    return True

if __name__ == "__main__":
    success = setup_database()
    sys.exit(0 if success else 1)