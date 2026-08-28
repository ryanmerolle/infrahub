import uuid

from infrahub.core.branch import Branch
from infrahub.core.constants import InfrahubKind
from infrahub.core.manager import NodeManager
from infrahub.core.migrations.graph.m077_delete_orphaned_account_children import Migration077
from infrahub.core.migrations.shared import MigrationInput
from infrahub.core.node import Node
from infrahub.core.query import QueryType
from infrahub.core.schema.schema_branch import SchemaBranch
from infrahub.database import InfrahubDatabase
from infrahub.database.validation import verify_graph


async def _create_account_with_children(db: InfrahubDatabase, name: str, sub: str) -> tuple[Node, Node, Node]:
    account = await Node.init(db=db, schema=InfrahubKind.ACCOUNT)
    await account.new(db=db, name=name, account_type="User", password=str(uuid.uuid4()))
    await account.save(db=db)

    identity = await Node.init(db=db, schema=InfrahubKind.EXTERNALIDENTITY)
    await identity.new(db=db, sub=sub, provider_name="provider1", protocol="oidc", account=account.id)
    await identity.save(db=db)

    token = await Node.init(db=db, schema=InfrahubKind.ACCOUNTTOKEN)
    await token.new(db=db, token=f"token-{sub}", account=account.id)
    await token.save(db=db)

    return account, identity, token


async def _existing_ids(db: InfrahubDatabase, kind: str) -> set[str]:
    return {node.get_id() for node in await NodeManager.query(db=db, schema=kind)}


async def test_migration_077(
    db: InfrahubDatabase, default_branch: Branch, register_core_models_schema: SchemaBranch
) -> None:
    """The children of a deleted account are removed, the children of a live account are kept."""
    deleted_account, orphaned_identity, orphaned_token = await _create_account_with_children(
        db=db, name="Deleted User", sub="sub-orphan-001"
    )
    live_account, live_identity, live_token = await _create_account_with_children(
        db=db, name="Live User", sub="sub-live-001"
    )

    # Reproduce what an account delete used to leave behind: the account gone, its children not.
    await deleted_account.delete(db=db)
    assert await _existing_ids(db=db, kind=InfrahubKind.EXTERNALIDENTITY) == {
        orphaned_identity.get_id(),
        live_identity.get_id(),
    }

    migration = Migration077()

    # A clean validation after the run only means something if it was dirty before.
    before = await migration.validate_migration(db=db)
    assert before.errors
    assert orphaned_identity.get_id() in " ".join(before.errors)
    assert orphaned_token.get_id() in " ".join(before.errors)
    assert live_identity.get_id() not in " ".join(before.errors)

    execution_result = await migration.execute(migration_input=MigrationInput(db=db))
    assert not execution_result.errors

    validation_result = await migration.validate_migration(db=db)
    assert not validation_result.errors

    assert await _existing_ids(db=db, kind=InfrahubKind.EXTERNALIDENTITY) == {live_identity.get_id()}
    assert await _existing_ids(db=db, kind=InfrahubKind.ACCOUNTTOKEN) == {live_token.get_id()}
    assert await _existing_ids(db=db, kind=InfrahubKind.ACCOUNT) == {live_account.get_id()}

    reloaded_identity = await NodeManager.get_one(db=db, id=live_identity.get_id(), raise_on_error=True)
    linked_account = await reloaded_identity.get_relationship(name="account").get_peer(db=db)
    assert linked_account is not None
    assert linked_account.get_id() == live_account.get_id()

    await verify_graph(db=db)

    second_result = await Migration077().execute(migration_input=MigrationInput(db=db))
    assert not second_result.errors
    assert await _existing_ids(db=db, kind=InfrahubKind.EXTERNALIDENTITY) == {live_identity.get_id()}
    assert await _existing_ids(db=db, kind=InfrahubKind.ACCOUNTTOKEN) == {live_token.get_id()}


async def test_migration_077_no_orphans(
    db: InfrahubDatabase, default_branch: Branch, register_core_models_schema: SchemaBranch
) -> None:
    """With every account still in place, nothing is deleted."""
    account, identity, token = await _create_account_with_children(db=db, name="Kept User", sub="sub-kept-001")

    migration = Migration077()
    assert not (await migration.validate_migration(db=db)).errors

    execution_result = await migration.execute(migration_input=MigrationInput(db=db))
    assert not execution_result.errors

    assert await _existing_ids(db=db, kind=InfrahubKind.EXTERNALIDENTITY) == {identity.get_id()}
    assert await _existing_ids(db=db, kind=InfrahubKind.ACCOUNTTOKEN) == {token.get_id()}
    assert await _existing_ids(db=db, kind=InfrahubKind.ACCOUNT) == {account.get_id()}
    await verify_graph(db=db)


async def test_migration_077_identity_without_relationship_node(
    db: InfrahubDatabase, default_branch: Branch, register_core_models_schema: SchemaBranch
) -> None:
    """An identity whose Relationship node is gone is still an orphan.

    The account lookup finds nothing at all rather than finding an inactive edge, so the count it
    aggregates has to survive a match that produces no rows.
    """
    account, identity, _ = await _create_account_with_children(db=db, name="Broken User", sub="sub-broken-001")
    await account.delete(db=db)

    removed = await db.execute_query(
        query="""
MATCH (node:Node { uuid: $uuid })-[:IS_RELATED]-(rel:Relationship)
DETACH DELETE rel
RETURN count(rel) AS removed
        """,
        params={"uuid": identity.get_id()},
        type=QueryType.WRITE,
    )
    assert removed[0]["removed"] > 0

    migration = Migration077()
    before = await migration.validate_migration(db=db)
    assert identity.get_id() in " ".join(before.errors)

    execution_result = await migration.execute(migration_input=MigrationInput(db=db))
    assert not execution_result.errors
    assert not (await migration.validate_migration(db=db)).errors

    assert await _existing_ids(db=db, kind=InfrahubKind.EXTERNALIDENTITY) == set()
