def test_floe_imports():
    import floe

    assert callable(floe.run)
