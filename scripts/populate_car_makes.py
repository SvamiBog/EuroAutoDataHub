import asyncio
import json
import re
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select, text

from services.api_service.app.db.models import CarMake
from services.api_service.app.core.config import settings

# Database connection details (using settings from config.py)
DB_USER = settings.POSTGRES_USER
DB_PASSWORD = settings.POSTGRES_PASSWORD
DB_NAME = settings.POSTGRES_DB
DB_HOST = settings.POSTGRES_SERVER
DB_PORT = settings.POSTGRES_PORT

DATABASE_URL = f"postgresql+asyncpg://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionFactory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

def generate_slug(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r'[\\s\\-]+', '-', s)  # Replace whitespace and hyphens with a single hyphen
    s = re.sub(r'[^\\w\\-]+', '', s)    # Remove non-alphanumeric characters (except hyphens)
    return s

async def populate_makes_from_json():
    json_file_path = "/home/maksim/projects/EuroAutoDataHub/services/scrapy_spiders/car_scrapers/car_scrapers/make_car.json"
    added_count = 0
    skipped_count = 0
    skipped_non_dict_count = 0
    error_count = 0

    print("Starting script to populate car makes...")
    print("Please ensure the following environment variables are set for database connection:")
    print("POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB, POSTGRES_SERVER, POSTGRES_PORT")
    print(f"Connecting to database: {DB_HOST}:{DB_PORT}/{DB_NAME} as user {DB_USER}")

    try:
        with open(json_file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        # Filter out comment lines (starting with //) and join the rest
        json_content_str = "".join([line for line in lines if not line.strip().startswith("//")])
        
        if not json_content_str.strip():
            print(f"Error: No valid JSON content found in {json_file_path} after stripping comments.")
            return

        data = json.loads(json_content_str) # Parse the cleaned string
        print(f"Successfully loaded {len(data)} items from {json_file_path} after cleaning comments.")

    except FileNotFoundError:
        print(f"Error: JSON file not found at {json_file_path}")
        return
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON from {json_file_path} after cleaning comments: {e}")
        print(f"Problematic content snippet (first 500 chars): {json_content_str[:500]}")
        return
    except Exception as e:
        print(f"An unexpected error occurred while reading or parsing {json_file_path}: {e}")
        return

    async with AsyncSessionFactory() as session:
        async with session.begin():
            try:
                # Test connection
                await session.execute(text("SELECT 1"))
                print("Database connection successful.")
            except Exception as e:
                print(f"Database connection failed: {e}")
                return

            # Iterate over the list of dictionaries
            for item in data:
                if isinstance(item, dict) and item.get("name") == "filter_enum_make":
                    make_name = item.get("value")
                    make_slug = item.get("canonical")

                    if not make_name:
                        print(f"Skipping item due to missing 'value' (make name): {item}")
                        error_count += 1
                        continue

                    if not make_slug:
                        make_slug = generate_slug(make_name)
                        print(f"Generated slug '{make_slug}' for make '{make_name}' as 'canonical' was missing.")

                    try:
                        # Check if make with this slug already exists
                        stmt = select(CarMake).where(CarMake.slug == make_slug)
                        result = await session.execute(stmt)
                        existing_make = result.scalars().first()

                        if existing_make:
                            # print(f"Make '{make_name}' with slug '{make_slug}' already exists. Skipping.")
                            skipped_count += 1
                        else:
                            new_make = CarMake(name=make_name, slug=make_slug)
                            session.add(new_make)
                            # print(f"Adding make: {make_name} (slug: {make_slug})")
                            added_count += 1
                    except Exception as e:
                        print(f"Error processing item {item}: {e}")
                        error_count += 1
                        # Optionally, rollback this specific item or collect errors
                elif isinstance(item, dict):
                    # print(f"Skipping item with unexpected 'name' value or structure: {item.get('name')}")
                    skipped_non_dict_count +=1 # This counter name is a bit misleading now, but keeps logic similar
                else:
                    print(f"Skipping non-dictionary or unexpected item: {item}")
                    skipped_non_dict_count += 1
                    continue
            
            if error_count > 0:
                print(f"There were {error_count} errors during processing. Rolling back changes.")
                await session.rollback() # Rollback if any errors occurred during item processing
            else:
                await session.commit() # Commit only if all items processed without error
                print("Successfully committed changes to the database.")


    print("\\n--- Population Summary ---")
    print(f"Makes added: {added_count}")
    print(f"Makes skipped (already exist): {skipped_count}")
    print(f"Items skipped (unexpected format/name or non-dictionary): {skipped_non_dict_count}")
    print(f"Errors during processing: {error_count}")
    print("------------------------")

if __name__ == "__main__":
    asyncio.run(populate_makes_from_json())
