--!strict

local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local RunService = game:GetService("RunService")
local ServerScriptService = game:GetService("ServerScriptService")

local CLIENT_NAME = "VideoPlayerClient"

local Bruvo = {}
local started = false

local function installShared(): Folder
	local existing = ReplicatedStorage:FindFirstChild("Shared")
	if existing and existing:IsA("Folder") then
		return existing
	end
	local shared = script:WaitForChild("Shared"):Clone() :: Folder
	shared.Parent = ReplicatedStorage
	return shared
end

local function installClient(player: Player, bundle: Folder)
	task.spawn(function()
		local gui = player:FindFirstChildOfClass("PlayerGui")
			or player:WaitForChild("PlayerGui", 30) :: PlayerGui?
		if not gui or gui:FindFirstChild(CLIENT_NAME) then
			return
		end
		local copy = bundle:Clone()
		copy.Name = CLIENT_NAME
		copy.Parent = gui
	end)
end

function Bruvo.bruvo(username: string?, encoderUrl: string?): boolean
	if not RunService:IsServer() then
		error("bruvo() must run on the server", 2)
	end
	if started then
		return false
	end
	started = true

	local shared = installShared()
	local Config = require(shared:WaitForChild("Config")) :: any
	Config.OPERATOR = if username and username ~= "" then username else ""
	if encoderUrl and encoderUrl ~= "" then
		Config.ENCODER_URL = (encoderUrl:gsub("/+$", ""))
	end

	local bundle = script:WaitForChild("ClientBundle") :: Folder
	Players.PlayerAdded:Connect(function(player)
		installClient(player, bundle)
	end)
	for _, player in ipairs(Players:GetPlayers()) do
		installClient(player, bundle)
	end

	local server = script:WaitForChild("ServerScript"):Clone() :: Script
	server.Name = "VideoStreamServer"
	server.Disabled = false
	server.Parent = ServerScriptService

	return true
end

return Bruvo
