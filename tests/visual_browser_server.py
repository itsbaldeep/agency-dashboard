"""Synthetic loopback-only UI fixture. Never connects to the live database."""
from unittest import mock
from test_content_visuals import Database, dashboard
import content_pipeline  # Load canonical renderer before app's runtime import path.


class FixtureDatabase(Database):
    def fetchall(self):
        return []


if __name__ == '__main__':
    database = FixtureDatabase()
    with mock.patch.object(dashboard.models, 'db', return_value=database), mock.patch.object(dashboard.models, 'get_alert_nav_count', return_value=0):
        dashboard.app.run(host='127.0.0.1', port=5039, debug=False)
