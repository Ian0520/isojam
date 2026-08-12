from bs_roformer import BSRoformerSession

def create_model_session():
    session = BSRoformerSession(
    device="cuda",
    )
    session.load()
    return session