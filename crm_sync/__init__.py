import os

import click
from flask import Flask

from .extensions import db


def create_app(config=None):
    """Создаёт приложение Flask и подключает расширения."""
    app = Flask(__name__)
    app.config.from_mapping(
        SQLALCHEMY_DATABASE_URI=os.getenv(
            "DATABASE_URL",
            "postgresql+psycopg://postgres:postgres@localhost:5432/crm_sync",
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )

    if config:
        app.config.update(config)

    db.init_app(app)

    # Импорт регистрирует модели до вызова db.create_all().
    from . import models as _models  # noqa: F401

    if app.config.get("CRM_SOURCE") is not None:
        app.extensions["crm_source"] = app.config["CRM_SOURCE"]

    @app.cli.command("init-db")
    def init_db():
        """Создаёт таблицы проекта."""
        with app.app_context():
            db.create_all()

        click.echo("Database tables created.")

    @app.cli.command("rebuild-entity-index")
    def rebuild_entity_index_command():
        """Восстанавливает индекс по данным из JSONB."""
        from .sync.service import rebuild_entity_index

        with app.app_context():
            count = rebuild_entity_index()

        click.echo("Indexed {} CRM records.".format(count))

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app
