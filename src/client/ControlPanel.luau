--!strict

local UserInputService = game:GetService("UserInputService")
local Players = game:GetService("Players")

local TOGGLE_KEY = Enum.KeyCode.BackSlash

local ControlPanel = {}
ControlPanel.__index = ControlPanel

export type ControlPanel = typeof(setmetatable(
	{} :: {
		gui: ScreenGui,
		panel: Frame,
		box: TextBox,
		button: TextButton,
		stopButton: TextButton,
		status: TextLabel,
		onSubmit: (string) -> (),
		onStop: () -> (),
		busy: boolean,
	},
	ControlPanel
))

local function corner(parent: Instance, radius: number)
	local c = Instance.new("UICorner")
	c.CornerRadius = UDim.new(0, radius)
	c.Parent = parent
end

function ControlPanel.new(onSubmit: (string) -> (), onStop: () -> ()): ControlPanel
	local playerGui = Players.LocalPlayer:WaitForChild("PlayerGui")

	local gui = Instance.new("ScreenGui")
	gui.Name = "VideoControls"
	gui.IgnoreGuiInset = true
	gui.ResetOnSpawn = false
	gui.DisplayOrder = 10
	gui.ZIndexBehavior = Enum.ZIndexBehavior.Sibling

	local panel = Instance.new("Frame")
	panel.Name = "Panel"
	panel.AnchorPoint = Vector2.new(0.5, 1)
	panel.Position = UDim2.new(0.5, 0, 1, -24)
	panel.Size = UDim2.new(0, 620, 0, 88)
	panel.BackgroundColor3 = Color3.fromRGB(18, 18, 22)
	panel.BackgroundTransparency = 0.12
	panel.BorderSizePixel = 0
	panel.Parent = gui
	corner(panel, 10)

	local stroke = Instance.new("UIStroke")
	stroke.Color = Color3.fromRGB(70, 72, 84)
	stroke.Thickness = 1
	stroke.Parent = panel

	local status = Instance.new("TextLabel")
	status.Name = "Status"
	status.BackgroundTransparency = 1
	status.Position = UDim2.new(0, 14, 0, 8)
	status.Size = UDim2.new(1, -28, 0, 18)
	status.Font = Enum.Font.Gotham
	status.TextSize = 13
	status.TextXAlignment = Enum.TextXAlignment.Left
	status.TextColor3 = Color3.fromRGB(150, 154, 168)
	status.Text = "paste a video or GIF link  --  backslash hides this bar"
	status.Parent = panel

	local box = Instance.new("TextBox")
	box.Name = "Url"
	box.Position = UDim2.new(0, 14, 0, 32)

	box.Size = UDim2.new(1, -224, 0, 40)
	box.BackgroundColor3 = Color3.fromRGB(32, 33, 40)
	box.BorderSizePixel = 0
	box.Font = Enum.Font.Code
	box.TextSize = 14
	box.TextColor3 = Color3.fromRGB(235, 237, 245)
	box.PlaceholderText = "https://..."
	box.PlaceholderColor3 = Color3.fromRGB(110, 113, 126)
	box.Text = ""
	box.ClearTextOnFocus = false
	box.TextXAlignment = Enum.TextXAlignment.Left
	box.TextTruncate = Enum.TextTruncate.AtEnd
	box.Parent = panel
	corner(box, 6)

	local pad = Instance.new("UIPadding")
	pad.PaddingLeft = UDim.new(0, 10)
	pad.PaddingRight = UDim.new(0, 10)
	pad.Parent = box

	local button = Instance.new("TextButton")
	button.Name = "Play"
	button.AnchorPoint = Vector2.new(1, 0)
	button.Position = UDim2.new(1, -102, 0, 32)
	button.Size = UDim2.new(0, 100, 0, 40)
	button.BackgroundColor3 = Color3.fromRGB(64, 132, 255)
	button.BorderSizePixel = 0
	button.Font = Enum.Font.GothamBold
	button.TextSize = 14
	button.TextColor3 = Color3.new(1, 1, 1)
	button.Text = "Play"
	button.AutoButtonColor = true
	button.Parent = panel
	corner(button, 6)

	local stopButton = Instance.new("TextButton")
	stopButton.Name = "Stop"
	stopButton.AnchorPoint = Vector2.new(1, 0)
	stopButton.Position = UDim2.new(1, -14, 0, 32)
	stopButton.Size = UDim2.new(0, 80, 0, 40)
	stopButton.BackgroundColor3 = Color3.fromRGB(54, 56, 66)
	stopButton.BorderSizePixel = 0
	stopButton.Font = Enum.Font.GothamBold
	stopButton.TextSize = 14
	stopButton.TextColor3 = Color3.fromRGB(228, 232, 240)
	stopButton.Text = "Stop"
	stopButton.AutoButtonColor = true
	stopButton.Parent = panel
	corner(stopButton, 6)

	gui.Parent = playerGui

	local self = setmetatable({
		gui = gui,
		panel = panel,
		box = box,
		button = button,
		stopButton = stopButton,
		status = status,
		onSubmit = onSubmit,
		onStop = onStop,
		busy = false,
	}, ControlPanel) :: any

	local function submit()

		local url = self.box.Text:gsub("^%s+", ""):gsub("%s+$", "")
		if url == "" then
			self:setStatus("enter a link first", true)
			return
		end
		if self.busy then
			return
		end
		self.onSubmit(url)
	end

	button.MouseButton1Click:Connect(submit)
	box.FocusLost:Connect(function(enterPressed)
		if enterPressed then
			submit()
		end
	end)

	stopButton.MouseButton1Click:Connect(function()

		self.onStop()
	end)

	UserInputService.InputBegan:Connect(function(input, processed)

		if not processed and input.KeyCode == TOGGLE_KEY then
			panel.Visible = not panel.Visible
		end
	end)

	return self
end

function ControlPanel.setStatus(self: ControlPanel, text: string, isError: boolean?)
	self.status.Text = text
	self.status.TextColor3 = if isError
		then Color3.fromRGB(255, 118, 118)
		else Color3.fromRGB(150, 154, 168)
end

function ControlPanel.setBusy(self: ControlPanel, busy: boolean)
	self.busy = busy
	self.button.Text = if busy then "..." else "Play"
	self.button.BackgroundColor3 = if busy
		then Color3.fromRGB(70, 74, 90)
		else Color3.fromRGB(64, 132, 255)
end

return ControlPanel
