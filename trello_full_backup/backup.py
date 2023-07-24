#!/usr/bin/env python3

import sys
import collections
import os
import argparse
import re
import datetime
import requests
import json
import traceback
import inflection

# Do not download files over 100 MB by default
ATTACHMENT_BYTE_LIMIT = 100000000
ATTACHMENT_REQUEST_TIMEOUT = 30  # 30 seconds
FILE_NAME_MAX_LENGTH = 100
FILTERS = ['open', 'all']

API = 'https://api.trello.com/1/'

# Read the API keys from the environment variables
API_KEY = os.getenv('TRELLO_API_KEY', '')
API_TOKEN = os.getenv('TRELLO_TOKEN', '')

auth = '?key={}&token={}'.format(API_KEY, API_TOKEN)


def mkdir(name):
    ''' Make a folder if it does not exist already '''
    if not os.access(name, os.R_OK):
        os.mkdir(name)
        
def purge_symlinks():
    ''' Remove all symlinks from the current folder '''
    for file in os.listdir():
        if os.path.islink(file):
            os.remove(file)

def get_extension(filename):
    ''' Get the extension of a file '''
    return os.path.splitext(filename)[1]


def get_name(tokenize, symlinks, real_name, backup_name, element_id=None):
    ''' Get back the name for the tokenize mode or the real name in the card.
        If there is an ID, keep it
    '''
    if tokenize:
        name = backup_name
    elif element_id is None:
        name = '{}'.format(sanitize_file_name(real_name, symlinks))
    else:    
        name = '{}_{}'.format(element_id, sanitize_file_name(real_name, symlinks))
    return name


def sanitize_file_name(name, ascii_only = False):
    ''' Stip problematic characters for a file name '''
    new_name = re.sub(r'[<>:\/\|\?\*\']', '_', name)[:FILE_NAME_MAX_LENGTH]
    if ascii_only:
        new_name = inflection.transliterate(new_name)  # Change accented characters to ascii
    return new_name


def write_file(file_name, obj, dumps=True):
    ''' Write <obj> to the file <file_name> '''
    with open(file_name, 'w', encoding='utf-8') as f:
        to_write = json.dumps(obj, indent=4, sort_keys=True) if dumps else obj
        f.write(to_write)


def filter_boards(boards, closed):
    ''' Return a list of the boards to retrieve (closed or not) '''
    return [b for b in boards if not b['closed'] or closed]


def download_attachments(c, max_size, tokenize=False, symlinks=False):
    ''' Download the attachments for the card <c> '''
    # Only download attachments below the size limit
    attachments = [a for a in c['attachments']
                   if a['bytes'] is not None and
                   (a['bytes'] < max_size or max_size == -1)]
    failures = []

    if len(attachments) > 0:
        # Enter attachments directory
        mkdir('attachments')
        os.chdir('attachments')
        if symlinks:
            purge_symlinks()        

        # Download attachments
        for id_attachment, attachment in enumerate(attachments):
            extension = get_extension(attachment["name"])
            # Keep the size in bytes to backup modifications in the file
            backup_name = '{}_{}{}'.format(attachment['id'],
                                           attachment['bytes'],
                                           extension)
            attachment_name = get_name(tokenize,
                                       symlinks,
                                       attachment["name"],
                                       backup_name,
                                       id_attachment)

            # We check if the file already exists, if it is the case we skip it
            if not os.path.isfile(attachment_name):
                print('Saving attachment', attachment_name)
                try:
                    content = requests.get(attachment['url'],
                                           stream=True,
                                           timeout=ATTACHMENT_REQUEST_TIMEOUT,
                                           headers={"Authorization":"OAuth oauth_consumer_key=\"{}\", oauth_token=\"{}\"".format(API_KEY, API_TOKEN)})
                    content.raise_for_status()
                except Exception as e:
                    sys.stderr.write('Failed download: {} - {}'.format(attachment_name, e))
                    failures.append((attachment_name, e))
                    continue

                with open(attachment_name, 'wb') as f:
                    for chunk in content.iter_content(chunk_size=1024):
                        if chunk:
                            f.write(chunk)
                            
            else:
                print('Attachment', attachment_name, 'exists already.')

            if symlinks:
                try:                     
                    os.symlink(attachment_name, get_name(False,
                                                         True,
                                                         attachment["name"],
                                                         backup_name, id_attachment))
                except FileExistsError:
                    pass

        # Exit attachments directory
        os.chdir('..')

    return failures


def backup_card(id_card, c, attachment_size, tokenize=False, symlinks=False):
    ''' Backup the card <c> with id <id_card> '''
    card_name = get_name(tokenize, symlinks, c["name"], c['id'], id_card)

    card_actions = requests.get(''.join((
        '{}cards/{}/actions{}&'.format(API, c["id"], auth)
    ))).json()

    mkdir(card_name)
    if symlinks:
        try:
            os.symlink(card_name, get_name(False, True, c["name"], c['id'], id_card))
        except FileExistsError:
            pass

    # Enter card directory
    os.chdir(card_name)
    if symlinks:
        purge_symlinks()

    meta_file_name = 'card.json'
    description_file_name = 'description.md'
    actions_file_name = 'actions.json'
    comments_file_name = 'comments.md'
    comments = ''

    for id, clist_id in enumerate(c['idChecklists']):
        checkList = requests.get(
            ''.join(('{}checklists/{}{}&'.format(API, clist_id, auth))),
            'checkItems=all&'
            'checkItem_fields=all').text
        filename = 'checklist_' + clist_id + '.txt'
        write_file(filename, checkList, dumps=False)

    for action_id, action in enumerate(card_actions):
        if action['type'] == 'commentCard':
            action_date = action['date']
            comment_text = action['data']['text']
            username = action['memberCreator']['username']
            comments += (('date: {}\r\nusername: {}\r\ncomment: {}\r\n\r\n'.format(action_date, username, comment_text)))

    print('Saving', card_name)
    print('Saving', meta_file_name, 'and', description_file_name)
    write_file(meta_file_name, c)
    write_file(description_file_name, c['desc'], dumps=False)
    write_file(actions_file_name, card_actions)
    write_file(comments_file_name, comments, dumps=False)

    failed_attachments = download_attachments(c, attachment_size, tokenize, symlinks)

    # Exit card directory
    os.chdir('..')
    return [
        (os.path.join(card_name, "attachments", attchment_name), exception)
        for attchment_name, exception in failed_attachments
    ]


def backup_board(board, args):
    ''' Backup the board '''

    tokenize = bool(args.tokenize)
    symlinks = bool(args.symlinks)

    board_request = requests.get(''.join((
        '{}boards/{}{}&'.format(API, board["id"], auth),
        'actions=all&actions_limit=1000&',
        'cards={}&'.format(FILTERS[args.archived_cards]),
        'card_attachments=true&',
        'labels=all&',
        'lists={}&'.format(FILTERS[args.archived_lists]),
        'members=all&',
        'member_fields=all&',
        'checklists=all&',
        'fields=all'
    )))
    board_request.raise_for_status()
    board_details = board_request.json()

    board_dir = get_name(tokenize,
                         symlinks,
                         board_details['name'],
                         board_details['id'])

    mkdir(board_dir)
    
    if symlinks:
        try:
            os.symlink(board_dir, get_name(False,
                                           True,
                                           board_details['name'],
                                           board_details['id']))
        except FileExistsError:
            pass


    # Enter board directory
    os.chdir(board_dir)
    if symlinks:
        purge_symlinks()
    
    file_name = '{}_full.json'.format(board_dir)
    print('Saving full json for board',
          board_details['name'], 'with id', board['id'], 'to', file_name)
    write_file(file_name, board_details)

    lists = collections.defaultdict(list)
    for card in board_details['cards']:
        lists[card['idList']].append(card)
    for list_cards in lists.values():
        list_cards.sort(key=lambda card: card['pos'])

    failed_attachments = []
    for id_list, ls in enumerate(board_details['lists']):
        list_name = get_name(tokenize, symlinks, ls['name'], ls["id"], id_list)

        mkdir(list_name)

        if symlinks:
            try:
                os.symlink(list_name, get_name(False, True, ls['name'], ls["id"], id_list))
            except FileExistsError:
                pass
                

        # Enter list directory
        os.chdir(list_name)
        if symlinks:
            purge_symlinks()
        cards = lists[ls['id']]

        for id_card, c in enumerate(cards):
            card_failed_attachments = backup_card(id_card, c, args.attachment_size, tokenize, symlinks)
            failed_attachments.extend(
                [(os.path.join(list_name, attchment_path), exception)
                 for attchment_path, exception in card_failed_attachments])

        # Exit list directory
        os.chdir('..')

    # Exit sub directory
    os.chdir('..')

    if failed_attachments:
        raise Exception("Failed {} attachment downloads:\n{}".format(
            len(failed_attachments),
            "\n".join((path for path, e in failed_attachments))))


def cli():

    # Parse arguments
    parser = argparse.ArgumentParser(
        description='Trello Full Backup'
    )

    # The destination folder to save the backup to
    parser.add_argument('-d',
                        metavar='DEST',
                        nargs='?',
                        help='Destination folder')

    # incremental mode don't download the
    # already existing attachments
    parser.add_argument('-i', '--incremental',
                        dest='incremental',
                        action='store_const',
                        default=False,
                        const=True,
                        help='Backup incrementally (existing folder)')

    # Tokenize the names for folders and files
    parser.add_argument('-t', '--tokenize',
                        dest='tokenize',
                        action='store_const',
                        default=False,
                        const=True,
                        help='Name folders and files using the long ID')

    # Create links to tokens
    parser.add_argument('-s', '--symlinks',
                        dest='symlinks',
                        action='store_const',
                        default=False,
                        const=True,
                        help='Create named symlinks to tokens (on OSes that accept symlinks).')

    # Backup the boards that are closed
    parser.add_argument('-B', '--closed-boards',
                        dest='closed_boards',
                        action='store_const',
                        default=0,
                        const=1,
                        help='Backup closed board')

    # Backup the lists that are archived
    parser.add_argument('-L', '--archived-lists',
                        dest='archived_lists',
                        action='store_const',
                        default=0,
                        const=1,
                        help='Backup archived lists')

    # Backup the cards that are archived
    parser.add_argument('-C', '--archived-cards',
                        dest='archived_cards',
                        action='store_const',
                        default=0,
                        const=1,
                        help='Backup archived cards')

    # Backup my boards
    parser.add_argument('-m', '--my-boards',
                        dest='my_boards',
                        action='store_const',
                        default=False,
                        const=True,
                        help='Backup my personal boards')

    # Backup organizations
    parser.add_argument('-o', '--organizations',
                        dest='orgs',
                        action='store_const',
                        default=False,
                        const=True,
                        help='Backup organizations')

    # Set the size limit for the attachments
    parser.add_argument('-a', '--attachment-size',
                        dest='attachment_size',
                        nargs='?',
                        default=ATTACHMENT_BYTE_LIMIT,
                        type=int,
                        help='Attachment size limit in bytes. ' +
                        'Set to -1 to disable the limit')

    args = parser.parse_args()

    dest_dir = datetime.datetime.now().isoformat('_')
    dest_dir = '{}_backup'.format(dest_dir.replace(':', '-').split('.')[0])

    if args.d:
        dest_dir = args.d
        
    if bool(args.symlinks):
        args.tokenize = True

    if os.access(dest_dir, os.R_OK):
        if not bool(args.incremental):
            print('Folder', dest_dir, 'already exists')
            sys.exit(1)

    mkdir(dest_dir)

    os.chdir(dest_dir)
    if bool(args.symlinks):
        purge_symlinks()
    

    # If neither -m or -o args specified, default to my boards only
    if not (args.my_boards or args.orgs):
        args.my_boards = True
        print('No backup specified (-m and -o switches omitted). Backing up personal boards.')

    print('==== Backup initiated')
    print('Backing up to:', dest_dir)
    print('Incremental:', bool(args.incremental))
    print('Tokenize:', bool(args.tokenize))
    print('Backup my boards:', bool(args.my_boards))
    print('Backup organization boards:', bool(args.orgs))
    print('Backup closed board:', bool(args.closed_boards))
    print('Backup archived lists:', bool(args.archived_lists))
    print('Backup archived cards:', bool(args.archived_cards))
    print('Attachment size limit (bytes):', args.attachment_size)
    print('==== ')
    print()

    org_boards_data = {}

    if args.my_boards:
        my_boards_url = '{}members/me/boards{}'.format(API, auth)
        my_boards_request = requests.get(my_boards_url)
        my_boards_request.raise_for_status()
        org_boards_data['me'] = my_boards_request.json()

    orgs = []
    if args.orgs:
        org_url = '{}members/me/organizations{}'.format(API, auth)
        org_request = requests.get(org_url)
        org_request.raise_for_status()
        orgs = org_request.json()

    for org in orgs:
        boards_url = '{}organizations/{}/boards{}'.format(API, org['id'], auth)
        boards_request = requests.get(boards_url)
        boards_request.raise_for_status()
        org_boards_data[org['name']] = boards_request.json()

    # List of tuples (board, exception, formatted traceback)
    board_failures = []
    for org, boards in org_boards_data.items():
        mkdir(org)
        os.chdir(org)
        if bool(args.symlinks):
            purge_symlinks()
        boards = filter_boards(boards, args.closed_boards)
        for board in boards:
            try:
                backup_board(board, args)
            except Exception as e:
                board_failures.append((board, e, traceback.format_exc()))
        os.chdir('..')

    if board_failures:
        print()
        for board, exception, formatted_traceback in board_failures:
            print('Failed to backup board {} ({})'.format(
                board["id"], board["name"]))
            print(formatted_traceback)

        if len(board_failures) == 1:
            raise board_failures[0][1]
        else:
            raise Exception([exception for board, exception, formatted_traceback
                             in board_failures])

    print('Trello Full Backup Completed!')


if __name__ == '__main__':
    cli()
