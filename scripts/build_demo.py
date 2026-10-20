"""Generate the public demo from the repository's synthetic sample; no AI calls."""
import json
from config import SAMPLE_CSV, PROJECT_ROOT
from modules.bank_parser import parse_bank_csv
from modules.data_processor import add_flags
from modules.categoriser import rule_category
from modules.pipeline import _records


def main():
    frame = add_flags(parse_bank_csv(SAMPLE_CSV))
    frame['category'] = frame.apply(lambda r: 'Income' if r['flow'] == 'income' else
 'Transfers' if r['flow'] == 'transfer' else rule_category(r['description']) or 'Other', axis=1)
    (PROJECT_ROOT / 'frontend' / 'demo-data.json').write_text(json.dumps(_records(frame), indent=2) + '\n')


if __name__ == '__main__':
    main()
