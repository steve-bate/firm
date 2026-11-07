import pytest

from firm.core.auth.keys import create_key_pair, public_key_from_private


def test_derived_public_key_is_the_pair_that_was_created():
    """The published half of a key pair must be derivable from the private half.

    A server signs with the private key it stores and peers verify with the public key it
    publishes; a pair generated independently beside it leaves every peer's verifier with
    a key that cannot check the signature.
    """
    pair = create_key_pair()

    assert public_key_from_private(pair.private).strip() == pair.public.strip()


def test_deriving_from_something_that_is_not_a_private_key_is_refused():
    with pytest.raises(ValueError):
        public_key_from_private("")
    with pytest.raises(ValueError):
        public_key_from_private(create_key_pair().public)
