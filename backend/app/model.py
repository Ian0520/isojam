def create_model_session():
    # Inference is optional for API-only processes. Import it only when requested.
    try:
        from bs_roformer import BSRoformerSession
    except ModuleNotFoundError as error:
        if error.name != "bs_roformer":
            raise
        raise RuntimeError(
            "Local processing requires inference dependencies; install with "
            'python -m pip install -e ".[inference]"'
        ) from error

    session = BSRoformerSession(
        device="cuda",
    )
    session.load()
    return session
