import regex as re
import mwparserfromhell as mw
import pandas as pd
import numpy as np
import requests
import time
# functions to parse liquipedia data
class SectionNotFoundException(Exception):
    pass
class CouldNotReadJsonException(Exception):
    pass
def make_request(user, game, throttle,  page_name, action):
    headers = {
            "User-Agent": user,
            "Accept-Encoding": "gzip"
        }

    try:

        time.sleep(throttle)
        request_params={
                "action": action,
                "format": "json"
            }
        if action == "query":
            request_params["rvprop"] =  "content"
            request_params['rvslots'] = 'main'
            request_params['titles'] = page_name
            request_params['prop'] = 'revisions'
        else:
            request_params['page'] = page_name
        response = requests.get(
            f"https://liquipedia.net/{game}/api.php",
            headers=headers,
            params = request_params,
            timeout=10
        )
        response.raise_for_status()  

        try:
            if action == "query":
                response = response.json()['query']['pages']
                output_map = {}
                for id, page in response.items():
                    title = page['title']
                    raw_str = page['revisions'][0]['slots']['main']['*']
                    output_map[title.lower().strip().replace(" ", "_")] = raw_str
                return output_map
            #TODO: handling multiple html pages in one request? is this even possible
            json = response.json()
            response = response.json()['parse']
            title = response['title']
            raw_str = response['text']['*']
            return {title.lower().strip().replace(" ", "_"): raw_str}

        except KeyError as e:
            raise CouldNotReadJsonException(f"Could not Read JSON Request Result, indicating potential input string issues: {e}")
    except requests.exceptions.Timeout:
        raise TimeoutError("Request to Liquipedia API timed out.")
    except requests.exceptions.RequestException as e:
        raise ConnectionError(f"Request to Liquipedia API failed: {e}")
    except Exception as e:
        raise RuntimeError(f"Unexpected error in _make_request: {e}")
    

def parse_player_team_history(str):
    pattern = r"'''(.*?)'''\s*(.*?)(?=(?:'''|$))"

    matches = re.findall(pattern, str, flags=re.DOTALL)
    pattern = r"{{TH|(.*?)}}"
    game_teams = []
    for game, text in matches:
        pattern = r"\{\{TH\|.*?\}\}"
        #parse each team
        teams = re.findall(pattern, text, flags = re.DOTALL)
        
        for team in teams:
            team_dict = {}
            pattern = r"\{\{TH\|([^|]+?)\|([^|}]+)(?:\|([^}]+))?\}\}"
            match = re.match(pattern, team)
            if match:
                date_range = match.group(1).strip()  
                team_dict["team"] = match.group(2).strip()        
                
                team_dict['start'], team_dict['end'] = re.split(r"\s*[−–—]\s*", date_range, maxsplit=1)
                team_dict['status'] = match.group(3).strip() if match.group(3) else None
                team_dict['game'] = game 
                game_teams.append(team_dict)
    return pd.DataFrame(game_teams)
def parse_team_name(name):
    t = re.search(r"\{\{TeamOpponent\|([^}|]*)", name)
    return re.search(r"\{\{TeamOpponent\|([^}|]*)", name)[1] # type: ignore

def parse_map(map):
    wikicode = mw.parse(map)

    template = wikicode.filter_templates()[0]

    map_data = {param.name.strip(): param.value.strip_code().strip() for param in template.params}
    if len(map_data) == 0:
        raise Exception(f"No maps were found in {map}")
    return map_data


def parse_is_finished(finished_str):
    return finished_str == "true"
def parse_series_data(series_info, regex, cleaning_function = lambda x: x):
    parsed =re.findall(regex, str(series_info), re.DOTALL)

    #print(parsed)
    return {key: cleaning_function(value) for key, value in parsed}

def parse_series(series_info, game):
    #get games:
    pattern = r"(map\d+)\s*=\s*(\{\{Map\|.*?\}\})"
    matches = re.findall(pattern, str(series_info), re.DOTALL)
    matches = pd.DataFrame([parse_map(match_data) for match_data in matches])

    #get teams:
    pattern = r"(opponent\d+)\s*=\s*(\{\{TeamOpponent\|.*?\}\})"
    team_names = parse_series_data(series_info, pattern, parse_team_name)

    #get date:
    pattern = r"(date)=(.+?\{\{Abbr/[A-Z]+\}\})\|"
    date = parse_series_data(series_info, pattern)

    
    
    matches[['opponent_1', 'opponent_2']] = team_names['opponent1'], team_names['opponent2']

    matches['date'] = date['date'] if 'date' in date else None
    #game-specific stuff probably changes here
    if game == 'counterstrike':
        #get hltv id:
        pattern = r"(hltv)=([0-9]+)|"
        hltv_id = parse_series_data(series_info, pattern)
        matches['hltv_id'] = hltv_id['hltv'] if 'hltv' in hltv_id else None
    return matches


def parse_grouped_games(name, info, game):
    alldfs = []
    for subinfo in info:
        if isinstance(subinfo, str):
            subinfo = mw.parse(subinfo)
        for template in subinfo.filter_templates(recursive=True):
            if template.name.matches("Matchlist") or template.name.matches("SingleMatch"):
                for subtemplate in template.params:
                    if "title=" in str(subtemplate):
                        name = subtemplate.split("=")[1]
                    if "{{Match" in subtemplate or "{{SingleMatch" in subtemplate:
                        match_df = parse_series(subtemplate, game)
                        match_df['stage'] = name
                        alldfs.append(match_df)
    return alldfs

def parse_playoff_data(info, game):
    alldfs = []
    for subinfo in info:
        #first try to get stage name from the RxMxheader 
        regex = (
            r"(\|R\d+M\d+header=[^\n]+)"      #turn into tuple of (|RxMxheader=name, text)
            r"(.*?)"                          
            r"(?=\|R\d+M\d+header=|\Z)"       
        )
        #if header declarations are at top, just takes last RxMx - gives incorrect stage - TODO: fix
        if len(re.findall(regex, str(subinfo), re.DOTALL)) > 0:
            header_regex = r"\|R(\d+)M\d+header=([^\n]+)"
            #headers_list = re.findall(header_regex, str(subinfo))
            headers = dict(re.findall(header_regex, str(subinfo)))
            if len(headers) > 1:
                #should have multiple headers,
                for round_num, stage_name in headers.items():
                    # Find all matches for this round
                    regex = (
                            r"\|R" + str(round_num) +
                            r"M\d+=\s*(\{\{Match.*?\}\})\s*(?=\|R" +
                            str(round_num) + r"M\d+=|\|R\d+M\d+=|\Z)"
                        )
                    round_matches = re.findall(regex, str(subinfo), re.DOTALL)
                    for match_text in round_matches:
                        match_df = parse_series(match_text, game)
                        match_df['stage'] = stage_name
                        alldfs.append(match_df)
            else: #if not, parse manually up to down - dealing with issues where two headers are marked R1 but in different places
                stages = parse_series_data(subinfo, regex)
                for stage, text in stages.items():
                    stage = stage.split("=")[1]
                    matches = re.split(r'\|R\d+M\d+=', text)
                    for match in matches:
                        if "{{Match" in match:
                            match_df = parse_series(match, game)
                            match_df['stage'] = stage
                            alldfs.append(match_df)
        else:
            #if fails, look at <!--stage-->
            #can be situations where <!--x--> is being used as a temporary value for an event that has not happened yet
            #i dont like the idea of hardcoding the terms "stage", "match", etc. but need to find a good way to deal with this TODO
            regex =  r'<!--\s*(.*?)\s*-->\s*(.*?)(?=\s*<!--\s*\w+|$)'
            stages = parse_series_data(subinfo, regex)

            for stage, text in stages.items():
                matches = re.split(r'\|R\d+M\d+=', text)
                for match in matches:
                    if "{{Match" in match:
                        match_df = parse_series(match, game)
                        match_df['stage'] = stage
                        alldfs.append(match_df)
    return alldfs
def parse_game_groups(stage, info, game):
    if "Bracket" in info[0]:
        new_games =  pd.concat(parse_playoff_data(info, game))
    else:
        new_games =  pd.concat(parse_grouped_games(stage, info, game))
    return new_games

def parseTeam(text):
    #get name
    match = re.search(r"\|\s*team\s*=\s*([^|}]+)", text)
    team = match.group(1).strip() if match else None
        
    #get qualification method
    match = re.search(r"\|\s*qualifier\s*=\s*([^|}]+)", text)
    qualifier =  match.group(1).strip("/[") if match else None

    match = re.findall(r"\b(p\d+|c)\s*=\s*([^\s|}]+)", text)
    players = {k: v for k, v in match} if match else None
    #find dnps
    match = re.search(r"\b(xxdnp)\s*=\s*(true)\b", text)
    dnps = (value for key, value in  match.groups()) if match else ()
        
    team_dict = {"team": team, "qualifier": qualifier, "dnps": dnps}
    team_dict.update(players) # type: ignore
    return pd.Series(team_dict)

#get broadcast talent
def parseBroadcaster(text):
    #get broadcast role
    match = re.search(r"\|\s*position\s*=\s*([^|}]+)", text)
    position = match.group(1).strip() if match else None
        
    #get broadcast language
    match = re.search(r"\|\s*lang\s*=\s*([^|}]+)", text)
    language =  match.group(1).strip() if match else None

    match = re.findall(r"\|b\d+\s*=\s*([^\|}]+)", text)
    names = [m.strip() for m in match]

    return pd.DataFrame(data = {
        "name": names,
        "language": [language] * len(names),
        "position": [position] * len(names)
    })
def parse_prizes(text):
    slots_raw = re.findall(r"\{\{Slot\|([^}]*)\}\}", str(text))

    #slots = [(slot.split("=")[0], slot.split("=")[1]) for slot in slots]
    slots_tuples = [
        re.findall(r"(\w+)=([^|]+)", slot)  # captures key and value
        for slot in slots_raw
    ]
    match = re.findall(r"(qualifies\d+name)=([^\|}]+)", str(text))
    #build maping of future qualifying events
    qualifications = {re.sub(r"qualifies(\d+)name", r"qualified\1", k): v.strip() for k, v in match}
    qualifications.update({"none":None})
    dict_rows = []
    for pairs in slots_tuples:
        slot_dict = dict(pairs)
        for key in list(slot_dict.keys()):
            if re.match(r"qualified\d+", key):
                slot_dict["qualifying"] = key  
                slot_dict.pop('key', None)

        if "qualifying" not in slot_dict:
            slot_dict["qualifying"] = "none"

        dict_rows.append(slot_dict)
    df = pd.DataFrame(dict_rows)
    df['qualifying'] = df['qualifying'].map(qualifications)
    df['count'] = df['count'].fillna(1)
    df['teams'] = np.empty((len(df), 0)).tolist()
    return df

def parse_expanded_prize_pool(text):
    match = re.findall(r"\{\{prize pool slot\b(?:[^{}]|\{\{[^{}]*\}\})*\}\}", 
                    str(text), re.DOTALL | re.IGNORECASE)
    all_placements = []
    for placement in match:
        parts = [p.strip() for p in placement.split("|") if p.strip()]

        data = {}
        team_names = []

        for part in parts:
            if "=" in part:
                key, value = part.split("=", 1)
                data[key.strip()] = value.strip()
            else:
                # Standalone part is likely team name
                team_name = part.strip()
                team_names.append(team_name)
        data['team'] = [team for team in team_names if "{" not in team]
        data['localprize'] = data['localprize'].strip("[") if 'localprize' in data else None
        data = {
                (k.strip("{}") if isinstance(k, str) else k): 
                (v.strip("{}") if  isinstance(v, str) else v)
                for k, v in data.items()
            }
        all_placements.append(data)
    return pd.DataFrame(all_placements)

def get_name_content_map(text):

    pattern = r"\|name(\d+)=(.*)"
    key_mapping = dict(re.findall(pattern, text))
    #print(text)
    pattern = r"\|content(\d+)=\s*\n(.*?)(?=\n\|content\d+=|\n\|name\d+=|\n\}\})"
    values = dict(re.findall(pattern, text, flags=re.S))
    mapping = {key_mapping[k]: v for k,v in values.items()}
    return mapping

def parse_news_str(raw_str):
    pattern = r"<ref.*?>(.*?)</ref>"
    ref_content = re.findall(pattern, str(raw_str), flags=re.S)
    
    refs = []
    for ref in ref_content:
        # Find all key=value pairs
        pairs = re.findall(r"(\w+)=([^|}]+)", ref)
        
        # Build dictionary dynamically
        ref_dict = {k.strip(): v.strip("[ ]") for k, v in pairs}
        refs.append(ref_dict)

    entry = re.sub(r"\[\[([^\]]+)\]\]", r"\1", str(raw_str))#remove [[]]
    entry = re.sub(r"<ref.*?>(.*?)</ref>", "", entry)#remove reference
    entry = re.sub(r"(\*|')", "", entry).strip()
    entry = re.split(r"\s-\s", entry)
    if len(entry)  == 2:
        return {"date":entry[0], "text": entry[1], "references":refs}
    return -1

def get_lowest_subsections(section):
    """
    Recursively gets all lowest-level subsections for a section.
    """
    subsections = section.get_sections(include_lead=False, include_headings=True)[1:]
    if not subsections:
        return [section]
    
    lowest = []
    for sub in subsections:
        lowest.extend(get_lowest_subsections(sub))
    return lowest#rm duplicates

def parse_person(text):
    person = re.sub(r"<ref.*?>.*?</ref>|<ref.*?/>", "", text) #remove reference

    pattern = r'(\w+)\s*=\s*(.*?)(?=\|\w+\s*=|$)' 
    pairs = re.findall(pattern, person)
    person_dict = {k.strip(): v.strip() for k, v in pairs}
    
    joindate = person_dict['joindate'].split("|") if 'joindate' in person_dict else None
    if joindate and len(joindate) > 1:
        person_dict['joindate'] = joindate[1]
        person_dict['joindate_note'] = joindate[2]

    if 'tournament' in person_dict:
        tournaments = re.findall(r'\[\[[^\]|]+\|([^\]]+)\]\]', person)
        person_dict['tournament'] = tournaments
    return person_dict