import pytest

from dbt.contracts.graph.model_config import OnConfigurationChangeOption
from dbt.tests.util import (
    assert_message_in_logs,
    get_model_file,
    run_dbt,
    run_dbt_and_capture,
    set_model_file,
)
from tests.functional.materializations.materialized_view_tests.files import (
    MY_SEED,
    MY_TABLE,
    MY_MATERIALIZED_VIEW,
    MY_VIEW,
)
from tests.functional.materializations.materialized_view_tests.utils import (
    query_indexes,
    query_relation_type,
    query_row_count,
    swap_indexes,
)


@pytest.fixture(scope="class", autouse=True)
def seeds():
    return {"my_seed.csv": MY_SEED}


@pytest.fixture(scope="class", autouse=True)
def models():
    yield {
        "my_table.sql": MY_TABLE,
        "my_view.sql": MY_VIEW,
        "my_materialized_view.sql": MY_MATERIALIZED_VIEW,
    }


@pytest.fixture(scope="class", autouse=True)
def setup(project):
    run_dbt(["seed"])
    yield


def test_materialized_view_create(project, my_materialized_view):
    assert query_relation_type(project, my_materialized_view) is None
    run_dbt(["run", "--models", my_materialized_view.name])
    assert query_relation_type(project, my_materialized_view) == "materialized_view"


def test_materialized_view_create_idempotent(project, my_materialized_view):
    assert query_relation_type(project, my_materialized_view) is None
    run_dbt(["run", "--models", my_materialized_view.name])
    assert query_relation_type(project, my_materialized_view) == "materialized_view"
    run_dbt(["run", "--models", my_materialized_view.name])
    assert query_relation_type(project, my_materialized_view) == "materialized_view"


def test_materialized_view_full_refresh(project, my_materialized_view):
    run_dbt(["run", "--models", my_materialized_view.name])
    _, logs = run_dbt_and_capture(
        ["--debug", "run", "--models", my_materialized_view.name, "--full-refresh"]
    )
    assert query_relation_type(project, my_materialized_view) == "materialized_view"
    assert_message_in_logs(
        f"Applying REPLACE to: {my_materialized_view.fully_qualified_path}", logs
    )


def test_materialized_view_replaces_table(project, my_materialized_view, my_table):
    run_dbt(["run", "--models", my_table.name])
    project.run_sql(
        f"""
        alter table {my_table.fully_qualified_path}
        rename to {my_materialized_view.name}
    """
    )
    assert query_relation_type(project, my_materialized_view) == "table"
    run_dbt(["run", "--models", my_materialized_view.name])
    assert query_relation_type(project, my_materialized_view) == "materialized_view"


def test_materialized_view_replaces_view(project, my_materialized_view, my_view):
    run_dbt(["run", "--models", my_view.name])
    project.run_sql(
        f"""
        alter view {my_view.fully_qualified_path}
        rename to {my_materialized_view.name}
    """
    )
    assert query_relation_type(project, my_materialized_view) == "view"
    run_dbt(["run", "--models", my_materialized_view.name])
    assert query_relation_type(project, my_materialized_view) == "materialized_view"


def test_view_replaces_materialized_table(project, my_materialized_view, my_table):
    run_dbt(["run", "--models", my_materialized_view.name])
    project.run_sql(
        f"""
        alter materialized view {my_materialized_view.fully_qualified_path}
        rename to {my_table.name}
    """
    )
    assert query_relation_type(project, my_table) == "materialized_view"
    run_dbt(["run", "--models", my_table.name])
    assert query_relation_type(project, my_table) == "table"


def test_view_replaces_materialized_view(project, my_materialized_view, my_view):
    run_dbt(["run", "--models", my_materialized_view.name])
    project.run_sql(
        f"""
        alter materialized view {my_materialized_view.fully_qualified_path}
        rename to {my_view.name}
    """
    )
    assert query_relation_type(project, my_view) == "materialized_view"
    run_dbt(["run", "--models", my_view.name])
    assert query_relation_type(project, my_view) == "view"


def test_materialized_view_only_updates_after_refresh(project, my_materialized_view, my_seed):
    run_dbt(["run", "--models", my_materialized_view.name])

    # poll database
    table_start = query_row_count(project, my_seed)
    view_start = query_row_count(project, my_materialized_view)

    # insert new record in table
    project.run_sql(f"insert into {my_seed.fully_qualified_path} (id, value) values (4, 400);")

    # poll database
    table_mid = query_row_count(project, my_seed)
    view_mid = query_row_count(project, my_materialized_view)

    # refresh the materialized view
    project.run_sql(f"refresh materialized view {my_materialized_view.fully_qualified_path};")

    # poll database
    table_end = query_row_count(project, my_seed)
    view_end = query_row_count(project, my_materialized_view)

    # new records were inserted in the table but didn't show up in the view until it was refreshed
    assert table_start < table_mid == table_end
    assert view_start == view_mid < view_end


class OnConfigurationChangeBase:
    @pytest.fixture(scope="class", autouse=True)
    def models(self):
        yield {
            "my_table.sql": MY_TABLE,
            "my_view.sql": MY_VIEW,
            "my_materialized_view.sql": MY_MATERIALIZED_VIEW,
        }

    @pytest.fixture(scope="function", autouse=True)
    def setup(self, project, my_materialized_view):
        run_dbt(["seed"])
        run_dbt(["run", "--models", my_materialized_view.name, "--full-refresh"])

        # the tests touch these files, store their contents in memory
        initial_model = get_model_file(project, my_materialized_view)

        yield

        # and then reset them after the test runs
        set_model_file(project, my_materialized_view, initial_model)


class TestOnConfigurationChangeApply(OnConfigurationChangeBase):
    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {"models": {"on_configuration_change": OnConfigurationChangeOption.Apply.value}}

    def test_index_updates_are_applied_with_alter(self, project, my_materialized_view):
        indexes = query_indexes(project, my_materialized_view)
        assert len(indexes) == 1
        assert indexes[0]["column_names"] == "id"

        swap_indexes(project, my_materialized_view)
        _, logs = run_dbt_and_capture(["--debug", "run", "--models", my_materialized_view.name])

        indexes = query_indexes(project, my_materialized_view)
        assert len(indexes) == 1
        assert indexes[0]["column_names"] == "value"  # this changed

        assert_message_in_logs(
            f"Applying ALTER to: {my_materialized_view.fully_qualified_path}", logs
        )
        assert_message_in_logs(
            f"Applying ALTER INDEXES to: {my_materialized_view.fully_qualified_path}", logs
        )
        assert_message_in_logs(
            f"Applying REPLACE to: {my_materialized_view.fully_qualified_path}", logs, False
        )


class TestOnConfigurationChangeContinue(OnConfigurationChangeBase):
    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {"models": {"on_configuration_change": OnConfigurationChangeOption.Continue.value}}

    def test_index_updates_are_not_applied(self, project, my_materialized_view):
        indexes = query_indexes(project, my_materialized_view)
        assert len(indexes) == 1
        assert indexes[0]["column_names"] == "id"

        swap_indexes(project, my_materialized_view)
        _, logs = run_dbt_and_capture(["--debug", "run", "--models", my_materialized_view.name])

        indexes = query_indexes(project, my_materialized_view)
        assert len(indexes) == 1
        assert indexes[0]["column_names"] == "id"  # this did not change

        assert_message_in_logs(
            f"Configuration changes were identified and `on_configuration_change` was set"
            f" to `continue` for `{my_materialized_view.fully_qualified_path}`",
            logs,
        )
        assert_message_in_logs(
            f"Applying ALTER to: {my_materialized_view.fully_qualified_path}", logs, False
        )
        assert_message_in_logs(
            f"Applying UPDATE INDEXES to: {my_materialized_view.fully_qualified_path}", logs, False
        )
        assert_message_in_logs(
            f"Applying REPLACE to: {my_materialized_view.fully_qualified_path}", logs, False
        )

    def test_index_updates_are_applied_on_full_refresh(self, project, my_materialized_view):
        indexes = query_indexes(project, my_materialized_view)
        assert len(indexes) == 1
        assert indexes[0]["column_names"] == "id"

        swap_indexes(project, my_materialized_view)
        _, logs = run_dbt_and_capture(
            ["--debug", "run", "--models", my_materialized_view.name, "--full-refresh"]
        )

        indexes = query_indexes(project, my_materialized_view)
        assert len(indexes) == 1
        assert indexes[0]["column_names"] == "value"  # this changed despite `continue`

        assert_message_in_logs(
            f"Applying ALTER to: {my_materialized_view.fully_qualified_path}", logs, False
        )
        assert_message_in_logs(
            f"Applying UPDATE INDEXES to: {my_materialized_view.fully_qualified_path}", logs, False
        )
        assert_message_in_logs(
            f"Applying REPLACE to: {my_materialized_view.fully_qualified_path}", logs, True
        )


class TestOnConfigurationChangeFail(OnConfigurationChangeBase):
    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {"models": {"on_configuration_change": OnConfigurationChangeOption.Fail.value}}

    def test_index_updates_are_not_applied(self, project, my_materialized_view):
        indexes = query_indexes(project, my_materialized_view)
        assert len(indexes) == 1
        assert indexes[0]["column_names"] == "id"

        swap_indexes(project, my_materialized_view)
        _, logs = run_dbt_and_capture(
            ["--debug", "run", "--models", my_materialized_view.name], expect_pass=False
        )

        indexes = query_indexes(project, my_materialized_view)
        assert len(indexes) == 1
        assert indexes[0]["column_names"] == "id"  # this did not change

        assert_message_in_logs(
            f"Configuration changes were identified and `on_configuration_change` was set"
            f" to `fail` for `{my_materialized_view.fully_qualified_path}`",
            logs,
        )
        assert_message_in_logs(
            f"Applying ALTER to: {my_materialized_view.fully_qualified_path}", logs, False
        )
        assert_message_in_logs(
            f"Applying UPDATE INDEXES to: {my_materialized_view.fully_qualified_path}", logs, False
        )
        assert_message_in_logs(
            f"Applying REPLACE to: {my_materialized_view.fully_qualified_path}", logs, False
        )

    def test_index_updates_are_applied_on_full_refresh(self, project, my_materialized_view):
        indexes = query_indexes(project, my_materialized_view)
        assert len(indexes) == 1
        assert indexes[0]["column_names"] == "id"

        swap_indexes(project, my_materialized_view)
        _, logs = run_dbt_and_capture(
            ["--debug", "run", "--models", my_materialized_view.name, "--full-refresh"]
        )

        indexes = query_indexes(project, my_materialized_view)
        assert len(indexes) == 1
        assert indexes[0]["column_names"] == "value"  # this changed despite `fail`

        assert_message_in_logs(
            f"Applying ALTER to: {my_materialized_view.fully_qualified_path}", logs, False
        )
        assert_message_in_logs(
            f"Applying UPDATE INDEXES to: {my_materialized_view.fully_qualified_path}", logs, False
        )
        assert_message_in_logs(
            f"Applying REPLACE to: {my_materialized_view.fully_qualified_path}", logs, True
        )
