"""Row-level security: the second line of defence. Queries in a user's session see only that
user's rows even when the code forgets to filter; without a user the query fails."""
import pytest
from conftest import post
from sqlalchemy import select, text


def _ids():
    from central import db
    from central.models import User
    with db.system_session() as s:
        return {u.email: u.id for u in s.scalars(select(User))}


def test_unfiltered_queries_see_only_own_rows(alice, bob):
    post(alice, "/uj", {"url": "https://youtu.be/AAAAAAAAAAA", "mode": "captions"})
    post(bob, "/uj", {"url": "https://youtu.be/BBBBBBBBBBB", "mode": "captions"})
    post(bob, "/uj", {"url": "https://youtu.be/AAAAAAAAAAA", "mode": "captions"})  # same video, own item
    ids = _ids()
    from central import db
    with db.user_session(ids["alice@example.com"]) as s:
        assert s.execute(text("SELECT count(*) FROM items")).scalar() == 1
        assert s.execute(text("SELECT count(*) FROM jobs")).scalar() == 1
        assert s.execute(text("SELECT count(*) FROM sources")).scalar() == 1  # only through own items
        # writing a row for someone else is refused
        with pytest.raises(Exception, match="row-level security"):
            s.execute(text("UPDATE jobs SET user_id = :b"), {"b": ids["bob@example.com"]})
    with db.user_session(ids["bob@example.com"]) as s:
        assert s.execute(text("SELECT count(*) FROM items")).scalar() == 2


def test_without_a_user_the_query_fails(alice):
    from central import db
    with pytest.raises(Exception, match="dwyt.user_id|invalid input syntax for type bigint"):
        with db.app_engine().connect() as conn:
            conn.execute(text("SELECT count(*) FROM items")).scalar()


def test_app_role_cannot_touch_auth_tables(alice):
    from central import db
    with pytest.raises(Exception, match="permission denied"):
        with db.user_session(1) as s:
            s.execute(text("SELECT count(*) FROM users")).scalar()


def test_other_users_item_page_is_not_found(alice, bob):
    r = post(alice, "/uj", {"url": "https://youtu.be/AAAAAAAAAAA", "mode": "captions"})
    item_url = r.headers["location"]
    assert alice.get(item_url).status_code == 200
    assert bob.get(item_url).status_code == 404
    assert post(bob, item_url + "/torles").status_code == 303
    assert alice.get(item_url).status_code == 200  # bob could not delete it
