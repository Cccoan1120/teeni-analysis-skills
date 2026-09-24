"""Extract styled preview rows from an XLSX without changing the full artifact."""
import argparse
import re
import xml.etree.ElementTree as ET
import zipfile

NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def extract(source, destination):
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as preview:
        for item in original.infolist():
            data = original.read(item.filename)
            if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', item.filename):
                root = ET.fromstring(data)
                limit = 26 if item.filename == 'xl/worksheets/sheet1.xml' else 5
                sheet_data = root.find('s:sheetData', NS)
                for row in list(sheet_data):
                    if int(row.get('r')) > limit:
                        sheet_data.remove(row)
                dimension = root.find('s:dimension', NS)
                if dimension is not None:
                    last = dimension.get('ref').split(':')[-1]
                    column = re.match('[A-Z]+', last).group()
                    dimension.set('ref', f'A1:{column}{limit}')
                for name in ('hyperlinks', 'mergeCells'):
                    parent = root.find('s:' + name, NS)
                    if parent is not None:
                        for child in list(parent):
                            if any(int(value) > limit for value in re.findall(r'\d+', child.get('ref', ''))):
                                parent.remove(child)
                data = ET.tostring(root, encoding='utf-8', xml_declaration=True)
            preview.writestr(item, data)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source')
    parser.add_argument('destination')
    args = parser.parse_args()
    extract(args.source, args.destination)
