import logging

from fastmcp import FastMCP
from fastmcp.server.transforms import ResourcesAsTools

from resources.windows_events import DateRange, get_users, get_top_events_by_user, \
    get_remote_access_events_by_user

mcp = FastMCP("elasticsearch")
logger = logging.getLogger(__name__)


@mcp.tool
async def get_windows_event_users(date_range: DateRange) -> str:
    """
    Query Windows events and return all users that feature in those events within the specified time range.
    :param date_range: Period for which to return events
    :return: List of user accounts featuring in events within the specified date range
    """
    return await get_users(date_range)


@mcp.tool
async def get_windows_events_by_user(user: str, size: int, date_range: DateRange) -> str:
    """
    Return Windows events relating to a specific user.
    :param user: Logon name (usually firstname.surname). Keyword field.
    :param size: Maximum number of events to return
    :param date_range: Period for which to return events
    :return: Windows events in Elastic Common Schema JSON format
    """
    return await get_top_events_by_user(user, size, date_range)


@mcp.tool
async def get_windows_remote_access_events_by_user(user: str, size: int, date_range: DateRange) -> str:
    """
    Return Microsoft Remote Desktop Gateway (RDG) and Remote Desktop Protocol (RDP) events relating to a specific user.
    These events contain records of user remote access sessions.
    :param user: Logon name (usually firstname.surname). Keyword field.
    :param size: Maximum number of events to return
    :param date_range: Period for which to return events
    :return: Windows events in Elastic Common Schema JSON format
    """
    return await get_remote_access_events_by_user(user, size, date_range)


if __name__ == '__main__':
    mcp.add_transform(ResourcesAsTools(mcp))
    mcp.run(
        transport='http',
        port=8000,
        host='0.0.0.0'
    )
