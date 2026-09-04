--!strict

local AssetService = game:GetService("AssetService")
local Players = game:GetService("Players")

local MAX_GRID_CELLS = 20736

local BUILD_CHUNK = 2000

local Renderer = {}
Renderer.__index = Renderer

export type Config = {
	pixelated: boolean?,
	showStats: boolean?,
	forceFrameGrid: boolean?,
}

export type Renderer = typeof(setmetatable(
	{} :: {
		width: number,
		height: number,
		backend: string,
		gui: ScreenGui,
		statsLabel: TextLabel?,
		size: Vector2,
		image: EditableImage?,
		cells: { Frame }?,
		offsets: { number }?,
		ready: boolean,
	},
	Renderer
))

local function corner(parent: Instance, radius: number)
	local c = Instance.new("UICorner")
	c.CornerRadius = UDim.new(0, radius)
	c.Parent = parent
end

local function tryEditableImage(width: number, height: number): EditableImage?
	local ok, image = pcall(function()
		return AssetService:CreateEditableImage({ Size = Vector2.new(width, height) })
	end)
	if not ok or not image then
		return nil
	end

	local probe = buffer.create(width * height * 4)
	local wrote = pcall(function()
		image:WritePixelsBuffer(Vector2.zero, Vector2.new(width, height), probe)
	end)
	if not wrote then
		pcall(function()
			image:Destroy()
		end)
		return nil
	end
	return image
end

local function gridSizeFor(width: number, height: number): (number, number)
	if width * height <= MAX_GRID_CELLS then
		return width, height
	end
	local scale = math.sqrt(MAX_GRID_CELLS / (width * height))
	return math.max(8, math.floor(width * scale)), math.max(8, math.floor(height * scale))
end

local function buildStats(gui: ScreenGui): TextLabel
	local label = Instance.new("TextLabel")
	label.Name = "Stats"
	label.Size = UDim2.new(0, 340, 0, 128)
	label.Position = UDim2.new(0, 8, 0, 8)
	label.BackgroundColor3 = Color3.new(0, 0, 0)
	label.BackgroundTransparency = 0.4
	label.TextColor3 = Color3.fromRGB(120, 255, 140)
	label.Font = Enum.Font.Code
	label.TextSize = 14
	label.TextXAlignment = Enum.TextXAlignment.Left
	label.TextYAlignment = Enum.TextYAlignment.Top
	label.Text = "starting..."
	label.Parent = gui
	return label
end

function Renderer.new(width: number, height: number, config: Config?): Renderer
	local cfg = config or {}
	local playerGui = Players.LocalPlayer:WaitForChild("PlayerGui")

	for _, existing in ipairs(playerGui:GetChildren()) do
		if existing:IsA("ScreenGui") and existing.Name == "VideoPlayer" then
			existing:Destroy()
		end
	end

	local gui = Instance.new("ScreenGui")
	gui.Name = "VideoPlayer"
	gui.IgnoreGuiInset = true
	gui.ResetOnSpawn = false
	gui.ZIndexBehavior = Enum.ZIndexBehavior.Sibling

	local backdrop = Instance.new("Frame")
	backdrop.Name = "Backdrop"
	backdrop.Size = UDim2.fromScale(1, 1)
	backdrop.BackgroundColor3 = Color3.new(0, 0, 0)
	backdrop.BorderSizePixel = 0
	backdrop.Parent = gui

	local surface = Instance.new("Frame")
	surface.Name = "Surface"
	surface.Size = UDim2.fromScale(1, 1)
	surface.Position = UDim2.fromScale(0.5, 0.5)
	surface.AnchorPoint = Vector2.new(0.5, 0.5)
	surface.BackgroundTransparency = 1
	surface.Parent = backdrop

	local aspect = Instance.new("UIAspectRatioConstraint")
	aspect.AspectRatio = width / height
	aspect.Parent = surface

	local self = setmetatable({
		width = width,
		height = height,
		backend = "none",
		gui = gui,
		statsLabel = if cfg.showStats then buildStats(gui) else nil,
		size = Vector2.new(width, height),
		image = nil,
		cells = nil,
		offsets = nil,
		ready = false,
	}, Renderer) :: any

	local image = if cfg.forceFrameGrid then nil else tryEditableImage(width, height)

	if image then
		self.backend = "editableimage"
		self.image = image

		local label = Instance.new("ImageLabel")
		label.Name = "Video"
		label.Size = UDim2.fromScale(1, 1)
		label.BackgroundTransparency = 1
		label.ScaleType = Enum.ScaleType.Fit
		label.ResampleMode = if cfg.pixelated
			then Enum.ResamplerMode.Pixelated
			else Enum.ResamplerMode.Default
		label.ImageContent = Content.fromObject(image)
		label.Parent = surface
		self.ready = true
	else
		self.backend = "framegrid"
		local gw, gh = gridSizeFor(width, height)

		local layout = Instance.new("UIGridLayout")
		layout.CellSize = UDim2.fromScale(1 / gw, 1 / gh)
		layout.CellPadding = UDim2.new()
		layout.SortOrder = Enum.SortOrder.LayoutOrder
		layout.FillDirectionMaxCells = gw
		layout.Parent = surface

		local count = gw * gh
		local cells = table.create(count)
		local offsets = table.create(count)
		local rowStride = width * 4

		for i = 1, count do
			local x = (i - 1) % gw
			local y = (i - 1) // gw

			local cell = Instance.new("Frame")
			cell.BorderSizePixel = 0
			cell.LayoutOrder = i
			cell.Parent = surface
			cells[i] = cell

			local sx = math.min(width - 1, (x * 2 + 1) * width // (gw * 2))
			local sy = math.min(height - 1, (y * 2 + 1) * height // (gh * 2))
			offsets[i] = sy * rowStride + sx * 4

			if i % BUILD_CHUNK == 0 then
				task.wait()
			end
		end

		self.cells = cells
		self.offsets = offsets
		self.ready = true
	end

	gui.Parent = playerGui
	return self
end

function Renderer.present(self: Renderer, frame: buffer): (boolean, string?)
	if not self.ready then
		return true, nil
	end

	if self.backend == "editableimage" then
		local image = self.image :: EditableImage
		local ok, err = pcall(function()
			image:WritePixelsBuffer(Vector2.zero, self.size, frame)
		end)
		if ok then
			return true, nil
		end
		return false, tostring(err)
	end

	local cells = self.cells :: { Frame }
	local offsets = self.offsets :: { number }
	for i = 1, #cells do
		local o = offsets[i]
		cells[i].BackgroundColor3 = Color3.fromRGB(
			buffer.readu8(frame, o),
			buffer.readu8(frame, o + 1),
			buffer.readu8(frame, o + 2)
		)
	end
	return true, nil
end

function Renderer.setStats(self: Renderer, text: string)
	local label = self.statsLabel
	if label then
		label.Text = text
	end
end

function Renderer.destroy(self: Renderer)
	self.gui:Destroy()
	local image = self.image
	if image then

		image:Destroy()
	end
	self.cells = nil
	self.offsets = nil
	self.ready = false
end

return Renderer
