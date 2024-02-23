import json
import uuid
from pathlib import Path
from typing import List, Any, Dict, Iterator

import requests
import xmltodict
from requests import Response

from src.constants import RSS_FEED_CSV_FIELDS_NAMES
from src.io import write_results_to_file
from src.utils import safe_get, unpack_singleton_list

RSS_FEED_DATA_DIRECTORY = Path(__file__).resolve().parents[1] / "data"
RSS_FEED_URL = "https://www.sec.gov/Archives/edgar/xbrlrss.all.xml"
RSS_COMPANY_TICKERS_FILE_PATH = RSS_FEED_DATA_DIRECTORY / "company_tickers.json"
RSS_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
UNKNOWN_TICKER_PLACEHOLDER = "UNKNOWN"


def _fetch_company_tickers(
    request_headers: Dict[str, Any], refresh_tickers_mapping: bool
) -> None:
    """
    Fetch the company tickers file from SEC website and save it to the data directory

    :param request_headers: headers to use for the request
    :param refresh_tickers_mapping: whether to refresh the tickers mapping file or not
    """

    # If tickers file is not present or refresh is requested, download the tickers file
    if not RSS_COMPANY_TICKERS_FILE_PATH.exists() or refresh_tickers_mapping:
        print(f"Downloading tickers file at {RSS_COMPANY_TICKERS_URL} ...")
        response = requests.get(RSS_COMPANY_TICKERS_URL, headers=request_headers)
        response.raise_for_status()
        mapping = response.json()
        cik_to_company_mapping = {}
        # Transform the tickers file data to {CIK: [tickers]} format
        print("Transforming tickers file to make it more easily usable ...")
        for _, company_data in mapping.items():
            if cik_to_company_mapping.get(company_data["cik_str"]) is None:
                cik_to_company_mapping[company_data["cik_str"]] = [
                    company_data["ticker"]
                ]
            else:
                cik_to_company_mapping[company_data["cik_str"]].append(
                    company_data["ticker"]
                )
        with open(RSS_COMPANY_TICKERS_FILE_PATH, "wb") as file:
            file.write(
                json.dumps(cik_to_company_mapping, indent=4, sort_keys=True).encode(
                    "utf-8"
                )
            )
        print(f"Successfully saved tickers file to {RSS_COMPANY_TICKERS_FILE_PATH}.")
    else:
        print(
            "Company tickers file found and no refresh requested, skipping download ..."
        )


def parse_rss_feed_data(
    response: Response,
    tickers: List[str],
    tickers_mapping: Dict[str, List[str]],
    files_urls_as_text: bool,
) -> Iterator[Dict[str, Any]]:
    """
    Parse the RSS feed data and yield the parsed data for each item

    :param response: response object containing the RSS feed data
    :param tickers: list of tickers to filter the parsed data with
    :param tickers_mapping: mapping of CIK numbers to company tickers
    :param files_urls_as_text: whether to group the XBRL files URLs in a newline-separated string or not.
    This results in the loss of part of the file information, but is more convenient e.g. for CSV format.

    :return: Iterator of parsed dicts for each item in the RSS feed
    """

    # Parse RSS feed data and get all items
    items = xmltodict.parse(response.content)["rss"]["channel"]["item"]
    for i in items:
        # Fetch the CIK number for current item
        cik = safe_get(i, "edgar:xbrlFiling", "edgar:cikNumber")

        # Removing leading zeros from CIK because it's not present in the SEC company tickers file,
        # while it is present in the RSS feed data
        trimmed_cik = cik.lstrip("0") if isinstance(cik, str) else None

        # Try fetching the ticker from the tickers mapping using trimmed CIK
        matching_tickers_for_item_cik: List[str] = tickers_mapping.get(trimmed_cik, [])

        # If tickers are provided by user, try extracting the matched ticker from the tickers mapping
        matched_ticker_str = next(
            (t for t in tickers if t in matching_tickers_for_item_cik), None
        )

        # If no matched ticker is found or no tickers are provided by user, concatenate the matching tickers
        # for the current CIK into a single string, if no matching tickers are found, use UNKNOWN as placeholder
        matched_ticker_str = (
            matched_ticker_str
            or "/".join(matching_tickers_for_item_cik)
            or UNKNOWN_TICKER_PLACEHOLDER
        )

        # Figure out whether to continue execution or discard current item based on the tickers
        # selection eventually provided by the user
        if tickers:
            # If trimmed CIK is not found in the tickers mapping, log and skip the current item
            if not matching_tickers_for_item_cik:
                print(
                    f"CIK {trimmed_cik} not found in tickers mapping, skipping item since we cannot tell "
                    f"whether it comes from one of the specified tickers..."
                )
                continue
            elif matched_ticker_str == UNKNOWN_TICKER_PLACEHOLDER:
                print(
                    f"CIK {trimmed_cik} could not be matched with any ticker, skipping item ..."
                )
                continue
            elif matched_ticker_str not in tickers:
                print(
                    f"Matched ticker(s) {matched_ticker_str} for CIK {trimmed_cik} not in specified tickers, skipping item ..."
                )
                continue

        # If current item is not skipped, parse it and yield the parsed data
        parsed_line = {
            "company_name": safe_get(i, "edgar:xbrlFiling", "edgar:companyName"),
            "cik": cik,
            "trimmed_cik": trimmed_cik,
            "ticker": matched_ticker_str,
            "published_date": i.get("pubDate"),
            "title": i.get("title"),
            "link": i.get("link"),
            "description": i.get("description"),
            "form": safe_get(i, "edgar:xbrlFiling", "edgar:formType"),
            "filing_date": safe_get(i, "edgar:xbrlFiling", "edgar:filingDate"),
            "file_number": safe_get(i, "edgar:xbrlFiling", "edgar:fileNumber"),
            "accession_number": safe_get(
                i, "edgar:xbrlFiling", "edgar:accessionNumber"
            ),
            "acceptance_date": safe_get(
                i, "edgar:xbrlFiling", "edgar:acceptanceDatetime"
            ),
            "period": safe_get(i, "edgar:xbrlFiling", "edgar:period"),
            "assistant_director": safe_get(
                i, "edgar:xbrlFiling", "edgar:assistantDirector"
            ),
            "assigned_sic": safe_get(i, "edgar:xbrlFiling", "edgar:assignedSic"),
            "fiscal_year_end": safe_get(i, "edgar:xbrlFiling", "edgar:fiscalYearEnd"),
        }

        # Process files URLs
        files_urls = safe_get(
            i, "edgar:xbrlFiling", "edgar:xbrlFiles", "edgar:xbrlFile"
        )
        if files_urls_as_text:
            files_urls = unpack_singleton_list([f.get("@edgar:url") for f in files_urls])
        parsed_line["xbrl_files"] = files_urls

        yield parsed_line


def fetch_rss_feed(
    tickers: List[str],
    output_file: str,
    refresh_tickers_mapping: bool,
) -> None:
    """
    Fetch the latest RSS feed data for the given company tickers and save it to either a CSV, JSON, or JSONLines file.

    :param tickers: list of company tickers to filter the RSS feed for
    :param output_file: name of the output file to save the results to
    :param refresh_tickers_mapping: whether to refresh the tickers mapping file or not
    """

    # Create the data directory if it doesn't exist
    RSS_FEED_DATA_DIRECTORY.mkdir(parents=True, exist_ok=True)

    # Uppercase and print the tickers to be fetched
    tickers = [x.upper() for x in tickers]
    print(f"Fetching RSS feed for tickers: {', '.join(tickers)}")

    # Create a User-Agent header
    headers = {
        "User-Agent": f"BellingcatEDGARTool_{uuid.uuid4()} contact-tech@bellingcat.com"
    }

    # Fetch the company tickers file if needed/requested
    _fetch_company_tickers(headers, refresh_tickers_mapping)

    # Load the JSON file for CIK numbers
    with open(RSS_COMPANY_TICKERS_FILE_PATH) as file:
        cik_to_ticker_mapping = json.load(file)

    # Fetch the RSS feed
    print(f"Fetching RSS feed from {RSS_FEED_URL}...")
    response = requests.get(RSS_FEED_URL, headers=headers)
    response.raise_for_status()

    # Parse the RSS feed data
    print("Parsing RSS feed XML data...")
    parsed_feed: Iterator[Dict[str, Any]] = parse_rss_feed_data(
        response,
        tickers,
        cik_to_ticker_mapping,
        True if output_file.lower().endswith(".csv") else False,
    )

    # Store the parsed data (simulating a generator to reuse the write_results_to_file function used in text search)
    print(f"Saving RSS feed data to {output_file}...")
    write_results_to_file(
        (parsed_feed for _ in range(1)), output_file, RSS_FEED_CSV_FIELDS_NAMES
    )
