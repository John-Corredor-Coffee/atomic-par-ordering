require 'webrick'
server = WEBrick::HTTPServer.new(
  Port: 7824,
  DocumentRoot: '/Users/johncorredor/Desktop/Vault/par-ordering'
)
trap('INT') { server.shutdown }
server.start
