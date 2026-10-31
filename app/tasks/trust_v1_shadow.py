"""
trust_v1_shadow.py: daily Trust Score v1 shadow run (app/services/trust_v1.py).

Stores v1 for every seller in trust_score_shadow. It never changes the public
score; that switches only with TRUST_SCORE_V1_LIVE. Scheduled once a day from
create_app through _run_in_context (Postgres advisory lock, one server at a
time). Safe to re-run: the same day's rows are updated, not duplicated.
"""
import logging

logger = logging.getLogger(__name__)


def run_trust_v1_shadow():
    from app.services.trust_v1 import run_shadow
    return run_shadow()


if __name__ == '__main__':
    from app import create_app
    app = create_app()
    with app.app_context():
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        print(run_trust_v1_shadow())
