"""Imported users must never be adopted by a newly allocated login account."""
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_auth as fixtures


@unittest.skipUnless(os.getenv("ECHOMIND_TEST_DATABASE_URL"), "Use random isolated auth test schema")
class IdentityAllocatorReviewTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.AuthStoreTests.asyncSetUp
    asyncTearDown = fixtures.AuthStoreTests.asyncTearDown

    async def test_stale_sequence_cannot_reuse_an_imported_users_identity(self):
        await self.pool.execute("INSERT INTO users(user_id) VALUES(120000)")
        imported = await self.pool.fetchval("SELECT count(*) FROM users WHERE user_id=120000")
        allocated = await self.store.create_account("independent-review", "Independent test password only")
        self.assertGreater(allocated, 120000)
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM users WHERE user_id=120000"), imported)
        self.assertFalse(await self.pool.fetchval("SELECT EXISTS(SELECT 1 FROM auth_accounts WHERE user_id=120000)"))
        self.assertEqual(await self.pool.fetchval("SELECT user_id FROM auth_accounts WHERE username='independent-review'"), allocated)


if __name__ == "__main__":
    unittest.main()
