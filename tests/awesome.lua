local awful = require("awful")
require("awful.autofocus")
local beautiful = require("beautiful")
beautiful.init({font="sans 10", border_width=0, useless_gap=0})
awful.layout.layouts = { awful.layout.suit.tile }
awful.screen.connect_for_each_screen(function(s)
    awful.tag({"test"}, s, awful.layout.suit.tile)
end)
awful.rules.rules = {
    { rule={}, properties={border_width=0, focus=awful.client.focus.filter,
        raise=true, screen=awful.screen.preferred, placement=awful.placement.no_offscreen} },
    -- Deliberately reproduce the user's generic Pqiv floating/ontop rule,
    -- with the intended media-browser instance exclusions.
    { rule={class="Pqiv"}, except_any={instance={"media-browser-gallery", "media-browser-viewer"}},
      properties={floating=true, ontop=true} },
}
client.connect_signal("manage", function(c) c:raise() end)
client.connect_signal("request::activate", function(c) client.focus=c; c:raise() end)
