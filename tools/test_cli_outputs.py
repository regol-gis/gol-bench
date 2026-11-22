import json
from pathlib import Path
import tempfile
import unittest

from benchmark_cli import compare, signature
from charts import application_figures, cli_figures


class CLIOutputsTests(unittest.TestCase):
    def test_csv_and_geojson_compare_payloads_across_order_and_encodings(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = [Path(tmp)/f'out.{fmt}' for fmt in ('csv', 'geojson', 'geojsonl')]
            csv_path, collection, lines = paths
            csv_path.write_text('id,name\r\nN1,"A,B"\r\nN2,"two\nlines"\r\n')
            csv_sig = signature(csv_path, 'csv', ['id','name'])
            csv_path.write_text('id,name\nN2,"two\nlines"\nN1,"A,B"\n')
            self.assertEqual(csv_sig, signature(csv_path, 'csv', ['id','name']))
            csv_path.write_text('id,name\nN1,"C,D"\nN2,"two\nlines"\n')
            self.assertNotEqual(csv_sig, signature(csv_path, 'csv', ['id','name']))
            with self.assertRaisesRegex(ValueError, 'columns'):
                signature(csv_path, 'csv', ['name','id'])
            rows = [dict(type='Feature', id=f'N{i}', properties={'name':str(i)},
                         geometry={'type':'Point','coordinates':[i,43.5]}) for i in range(2)]
            collection.write_text(json.dumps(dict(type='FeatureCollection', features=rows)))
            lines.write_text('\n'.join(json.dumps(r, sort_keys=True) for r in reversed(rows)))
            self.assertEqual(signature(collection, 'geojson', []), signature(lines, 'geojsonl', []))
            rows[0]['geometry']['coordinates'][1] = 45.0
            collection.write_text(json.dumps(dict(type='FeatureCollection', features=rows)))
            self.assertNotEqual(signature(collection, 'geojson', []), signature(lines, 'geojsonl', []))

    def test_geojson_tag_contracts_preserve_spelling_geometry_and_writer_differences(self):
        feature = ('{"type":"Feature","id":"N1","geometry":{"type":"Point","coordinates":[1.25,2]},'
                   '"properties":{"height":3.250,"layer":-2,"ref":"0012","name":"Café"}}')
        osm = feature.replace(':3.250', ':"3.250"').replace(':-2', ':"-2"')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'output'
            for fmt in ('geojson', 'geojsonl'):
                def read(row, **kwargs):
                    path.write_text('{"type":"FeatureCollection","features":[' + row + ']}'
                                    if fmt == 'geojson' else row + '\n', encoding='utf-8')
                    return signature(path, fmt, [], **kwargs)

                def check(actual, expected):
                    return compare([dict(signature=sig, runs=[{}]) for sig in (actual, expected)], 1)

                with self.subTest(format=fmt):
                    strict = read(feature)
                    reference = read(feature, osm_reference=True)
                    self.assertEqual(strict['fingerprint'], reference['fingerprint'])
                    self.assertIsNone(check(strict, strict))  # Earlier Regol, stored types.
                    self.assertIsNone(check(read(osm), reference))
                    # A second writer stored the same height as text. The GOL Tool
                    # outputs still differ, while the OSM string projections agree.
                    second = read(feature.replace(':3.250', ':"3.250"'), osm_reference=True)
                    self.assertNotEqual(second['fingerprint'], reference['fingerprint'])
                    self.assertEqual(second['osm_tags_fingerprint'], reference['osm_tags_fingerprint'])
                    for wrong in (feature, osm.replace('"3.250"', '"3.25"'), osm.replace('0012', '12'),
                                  osm.replace('1.25,2', '"1.25",2'), osm.replace('N1', 'N2')):
                        self.assertIn('differs', check(read(wrong), reference))
                    with self.assertRaisesRegex(ValueError, 'tag values'):
                        read(feature.replace(':3.250', ':true'), osm_reference=True)

    def test_cli_chart_excludes_mismatch_and_incomplete_arms(self):
        good = dict(signature={'selected':10}, runs=[dict(wall_ns=123, bytes=12)])
        bad = dict(signature={'selected':11}, runs=good['runs'])
        reason = compare([good, bad], 1)
        self.assertIn('differs', reason)
        self.assertIn('incomplete', compare([good, good], 2))
        figures, notes = cli_figures(dict(host='local', cpus=[2], samples=1, results=[
            dict(format='csv', query='n', verified=True, arms={'A':good,'B':good}),
            dict(format='count', query='w', verified=False, reason=reason)]))
        self.assertEqual(len(figures), 2)
        self.assertEqual(set(figures[0].values), {('n · csv','A'), ('n · csv','B')})
        self.assertIn('differs', notes[0])

    def test_ten_matched_workflows_share_one_reader_figure(self):
        manifest = Path(__file__).resolve().parents[1]/'benchmarks/application-sessions.json'
        cases = json.loads(manifest.read_text())['cases']
        row = dict(complete=True, digest='ok', signature=[1], runs=[[{'elapsed_ns':1234}]])
        results = {f"{case['id']}/{eng}" : row for case in cases
                   for eng in (('rust','python') if case['replay']=='reader' else ('rust-reader','python-reader'))}
        results['cesium/python'] = results['openplanetdata/rust-cli'] = row
        figures, _ = application_figures(dict(results=results, cpu='local', affinity=[1], samples=1))
        matched, = [f for f in figures if f.title.startswith('Matched reader')]
        self.assertEqual(len(matched.values), 20)
        self.assertEqual(len(figures), 3)


if __name__=='__main__':
    unittest.main()
