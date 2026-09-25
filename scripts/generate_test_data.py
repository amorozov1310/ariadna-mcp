"""
Generate test data: creates a project from fixtures for quick testing.
Usage: python scripts/generate_test_data.py [data_dir]
"""

import sys
import os
import shutil
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.project_manager import ProjectManager


def main():
    data_dir = sys.argv[1] if len(sys.argv) > 1 else './data'
    fixture = Path(__file__).parent.parent / 'tests' / 'fixtures' / 'report_ru.txt'

    if not fixture.exists():
        print(f"Error: fixture not found at {fixture}")
        sys.exit(1)

    print(f"Data dir: {data_dir}")
    pm = ProjectManager(data_dir)

    # Create test project if not exists
    project_id = 'pro100agro'
    try:
        pm.get_project(project_id)
        print(f"Project '{project_id}' already exists")
    except KeyError:
        pm.create_project(project_id, 'ПРО100АГРО', 'Тестовая конфигурация для отладки')
        print(f"Created project: {project_id}")

    # Add source
    project = pm.get_project(project_id)
    if not project.sources:
        source_dir = Path(data_dir) / 'projects' / project_id / 'sources' / 'main'
        source_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(fixture, source_dir / 'report.txt')
        pm.add_source(project_id, 'main', 'Основная конфигурация')
        print("Added source: main")

    # Index
    print("Indexing...")
    stats = pm.reindex(project_id)
    print(f"Done: {stats.total_objects} objects, {stats.total_attributes} attributes in {stats.duration_sec}s")

    pm.close_all()
    print(f"\nProject ready. Start server: DATA_DIR={data_dir} python -m src.main")


if __name__ == '__main__':
    main()
