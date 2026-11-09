-- Read-only. Reports only whether the given address belongs to a Mail account.
-- Never launches Mail and never returns other account details.
on run argv
	set target to item 1 of argv
	if application "Mail" is not running then return "not_running"
	tell application "Mail"
		set acctCount to count of accounts
		repeat with i from 1 to acctCount
			-- Copy the address list into a plain list before comparing; Mail refuses
			-- to coerce a live reference to one of its addresses into text.
			set addrs to (get email addresses of account i)
			repeat with j from 1 to count of addrs
				set addr to (item j of addrs) as text
				if addr is equal to target then
					if (get enabled of account i) then return "present_enabled"
					return "present_disabled"
				end if
			end repeat
		end repeat
	end tell
	return "absent"
end run
